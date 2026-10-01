# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Batch 2 item E / backlog #15: refusals never hold an unrelated finished task.

2026-09-30: the CTO repeated a refused finding-triage on in-scope findings of
logo-guided-choice-flow; the fifth refusal opened a hold on
audience-content-seo-expert-upgrade, the project's first task, released days
earlier, because the CTO's own task (GLOBAL_MONITOR) fell back to it.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from harness import board, board_surface, control
from tests import test_scaffold_wedge

_fixture = vars(test_scaffold_wedge.ScaffoldFixture)  # helpers only
REFUSAL = "only a needs_triage finding can be triaged"


def finding_record(finding_id: str, task: str, status: str) -> dict:
    """The shape board.record_finding writes, in the given status."""
    return {
        "id": finding_id, "fingerprint": finding_id, "task": task, "title": "Logo misses inline images",
        "description": "x", "evidence": "x", "status": status, "decision": None,
        "classification": "impacts_current_task" if status == "in_scope" else "unrelated_to_current_task",
        "created_at": board.now(), "decided_at": None, "next_action": "x",
    }


class RefusalHoldTaskTests(unittest.TestCase):
    setUp = _fixture["setUp"]
    begin_task = _fixture["begin_task"]

    def gateway(self):
        return board_surface.CommandGateway(self.context, board_surface.SessionTokenAuthority(self.context))

    def project(self):
        """As in #15: the alphabetically first task (the board sorts its keys) is an old released one."""
        old_agent, _ = self.begin_task("AUDIENCE-RELEASED")
        with board.locked_state(self.context) as state:
            state.setdefault("release_decisions", {})["AUDIENCE-RELEASED"] = {"task": "AUDIENCE-RELEASED", "decision": "accepted"}
            state["agents"][old_agent["id"]]["active"] = False
        self.begin_task("LOGO-LIVE")
        with board.locked_state(self.context) as state:
            state.setdefault("deferred_findings", {})["F-1"] = finding_record("F-1", "LOGO-LIVE", "in_scope")
        cto_session = control.create(self.context, "claude_cto")
        board.register(self.context, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=cto_session["id"])
        return SimpleNamespace(session_id=cto_session["id"])

    def repeat(self, identity, operation, arguments, times=None):
        gateway = self.gateway()
        for _ in range(times or board_surface.CommandGateway.REFUSAL_HOLD_THRESHOLD + 2):
            gateway._track_refusal(identity, operation, REFUSAL, arguments)
        return board.snapshot(self.context).get("control_plane_holds", {})

    def test_cto_repeated_finding_refusal_holds_the_findings_task_not_the_first_task(self):
        cto = self.project()
        holds = self.repeat(cto, "finding-triage", ["finding-triage", "--finding", "F-1", "--verdict", "repeat"])
        self.assertNotIn("AUDIENCE-RELEASED", holds)
        self.assertEqual(list(holds), ["LOGO-LIVE"])

    def test_no_hold_for_a_finished_task_an_unknown_task_or_a_read_only_command(self):
        cto = self.project()
        self.assertEqual(self.repeat(cto, "repin-final-review", ["repin-final-review", "--task", "AUDIENCE-RELEASED"]), {})
        self.assertEqual(self.repeat(cto, "push-confirm", ["push-confirm", "--instruction", "x"]), {})
        self.assertEqual(self.repeat(cto, "findings", ["findings", "--task", "LOGO-LIVE"]), {})

    def test_delivery_on_its_own_task_still_gets_the_hold(self):
        agent, _ = self.begin_task("SCAFFOLD-TASK")
        identity = SimpleNamespace(session_id=board.snapshot(self.context)["agents"][agent["id"]]["session_id"])
        holds = self.repeat(identity, "start-subtask", ["start-subtask", "--subtask", "alpha"])
        self.assertEqual(list(holds), ["SCAFFOLD-TASK"])


class FindingRefusalMessageTests(unittest.TestCase):
    setUp = _fixture["setUp"]

    def finding(self, status):
        with board.locked_state(self.context) as state:
            state.setdefault("deferred_findings", {})["F-9"] = finding_record("F-9", "T", status)

    def test_each_refusal_names_the_status_and_the_command_that_applies(self):
        cases = [
            ("in_scope", lambda: board.triage_finding(self.context, "F-9", "repeat"),
             "only a needs_triage finding can be triaged: finding F-9 is in_scope; use finding-resolved once the fix is re-tested"),
            ("deferred", lambda: board.resolve_finding(self.context, "F-9"),
             "finding F-9 is deferred; it waits for the owner's finding-decision (fix or do_not_fix)"),
            ("needs_triage", lambda: board.record_finding_decision(self.context, "F-9", "fix"),
             "finding F-9 is needs_triage; the CTO rules on it with finding-triage"),
            ("resolved", lambda: board.record_finding_decision(self.context, "F-9", "fix"),
             "finding F-9 is resolved; it is closed; no finding command applies"),
            ("fix_requested", lambda: board.triage_finding(self.context, "F-9", "distinct"),
             "finding F-9 is fix_requested; use finding-resolved"),
        ]
        for status, call, expected in cases:
            with self.subTest(status=status):
                self.finding(status)
                with self.assertRaises(ValueError) as caught:
                    call()
                self.assertIn(expected, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
