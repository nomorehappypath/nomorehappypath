# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A review that ends NOT TESTED is tracked as a gap and settled by the harness itself; the owner is never asked (windows-walk item 12,
changed by the owner on 2026-10-25: "You are now asking the user to sit in front on Mission Control waiting. BIG NO.").

2026-10-21: five review rounds on the owner's Windows project ended `TASK DONE: NOT TESTED` for one reason nobody could change and the board
said nothing. Until 2026-10-25 the fix was an owner card after round three; now the gap counts toward the review round limit, the one fresh
Delivery attempt follows, and an honest "could not finish" outcome is recorded (tests/test_frozen_review_scope.py). Machine inputs removed:
nothing is read from the machine (temporary project, real board).

Run:  PYTHONPATH=. python3 -m unittest tests.test_owner_told_when_stuck -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, project_manager, project_registry as registry
from tests import test_execute_once as base

OWNER_CAUSE = "TASK DONE: NOT TESTED — the access-denied scenario cannot occur on an administrator VM: every listener is queryable by an admin ssh session."
TOOL_CAUSE = "TASK DONE: NOT TESTED — my sandbox timed out before the browser started."


class GapTests(base.ExecuteOnceTests):
    def verdict(self, text, result="failed"):
        qa, request, _challenge, _evidence = self._claimed_review()
        board.execute_challenge(self.root, qa["id"], request["id"])
        board.qa_result(self.root, qa["id"], request["id"], result, text, "")
        return qa, request

    def state(self):
        return board.snapshot(self.root)

    def gaps(self):
        return list((self.state().get("verification_gaps") or {}).values())

    def cards(self):
        return [a for a in (self.state().get("owner_actions") or {}).values() if a.get("status") == "open"]

    def test_an_owner_controlled_cause_is_a_gap_and_never_a_card(self):
        self.verdict(OWNER_CAUSE)
        gap, = self.gaps()
        self.assertEqual((gap["state"], gap["rounds"]), ("diagnosing", 1))
        self.assertEqual(self.cards(), [], "the owner is not asked")
        home = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, home, True)
        alpha = registry.register(home, "alpha", self.root)
        registry.activate(home, alpha["id"])   # only the open project's board is read (owner 2026-10-28)
        row = project_manager.ProjectManager(home, board_port=0).projects_payload()["projects"][0]
        self.assertNotIn("Waiting for you", row["latest_progress"])
        self.assertIn("settling it by itself", row["latest_progress"])

    def test_repeats_stay_visible_to_the_cto_and_never_become_the_owners(self):
        self.verdict(TOOL_CAUSE)
        for number in (2, 3, 4):
            with board.locked_state(self.root) as live:
                qa = next(iter(live["agents"].values()))
                request = dict(next(iter(live["qa_requests"].values())), id=f"review-TASK-1-final-{number:02d}")
                board._note_verification_gap(self.root, live, request, qa, OWNER_CAUSE)
        gap, = self.gaps()
        self.assertEqual((gap["state"], gap["rounds"]), ("diagnosing", 4))
        self.assertEqual(self.cards(), [])
        note = board._verification_gap_note(self.state())
        self.assertIn("REPEATED NOT TESTED: round 4", note)
        self.assertIn("The owner is not asked", note)

    def test_lookalikes_raise_nothing(self):
        for text in ("TASK DONE: YES — all of it ran", "TASK DONE: NO — a real defect", "VERDICT: FAIL\nTASK DONE: NO — see the old text: TASK DONE: NOT TESTED",
                     "no declaration at all, just a failure", "The earlier round said TASK DONE: NOT TESTED but this one ran everything."):
            self.assertEqual(board._not_tested_declaration(text), "", text)
        self.assertTrue(board._not_tested_declaration("TASK DONE: NOT TESTED — x"))
        self.assertTrue(board._not_tested_declaration("  task done: not reproducible here, x"))
        self.verdict("TASK DONE: NO — a real defect")
        self.assertEqual(self.gaps(), [])
        self.assertEqual(self.cards(), [])

    def test_every_closing_path_closes_the_gap_and_keeps_the_history(self):
        for kind in ("task_cancelled", "git_acceptance_completed"):
            with self.subTest(kind=kind):
                self.setUp()
                self.verdict(OWNER_CAUSE)
                with board.locked_state(self.root) as live:
                    board._event(live, kind, None, {"task": "TASK-1", "message": "x"})
                gap, = self.gaps()
                self.assertEqual((gap["state"], gap["rounds"]), ("closed", 1))

    def test_a_passing_review_of_the_scope_closes_it(self):
        self.verdict(OWNER_CAUSE)
        with board.locked_state(self.root) as live:
            request = next(iter(live["qa_requests"].values()))
            board._close_gaps_for(live, "TASK-1", "Resolved: the scope passed review", board._gap_scope_key(request))
        gap, = self.gaps()
        self.assertEqual(gap["state"], "closed")


# the five recorded verdict declarations of the owner's project (shortened), each worded differently by the Reviewer
REAL = [
    "TASK DONE: NOT TESTED - the denied-access path was not observable in the reviewer credentialed process. The independent run failed.",
    "TASK DONE: NOT TESTED - the denied-access owner path remains unverified because the reviewer credentialed process produced no output.",
    "TASK DONE: NOT TESTED - the denied-access owner path remains unverified because it is not reproducible on the available admin VM.",
    "TASK DONE: NOT TESTED - the access-denied owner path remains untested: the available Windows VM account is an administrator.",
    "TASK DONE: NOT TESTED - the owner-required access-denied message was not exercised: the only available Windows account is an administrator.",
]


class ReplayTests(base.ExecuteOnceTests):
    """A board that holds NOT TESTED verdicts (and, from before 2026-10-25, an owner card) is rebuilt by the resume step."""

    def qa(self):
        return board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")

    def event(self, state, qa, request_id, result, message, at, test_scope="full"):
        state["qa_requests"][request_id] = {"id": request_id, "task": "APP", "phase": "final_acceptance", "status": result, "cycle": 1, "subtask": "", "chunk": "",
                                            "developer_id": "d", "claimed_by": qa["id"], "review_wait_started_at": at, "requested_at": at, "test_scope": test_scope, "structure_revision": 1}
        event = board._event(state, "qa_result", qa, {"task": "APP", "request_id": request_id, "result": result, "message": message})
        event["at"] = at
        return event

    def open_cards(self):
        return [a for a in (board.snapshot(self.root).get("owner_actions") or {}).values() if a.get("status") == "open"]

    def test_the_recorded_rounds_are_replayed_once_and_never_raise_a_card(self):
        qa = self.qa()
        with board.locked_state(self.root) as state:
            for round_number in range(1, 6):
                board._event(state, "qa_result", qa, {
                    "task": "why-port-cli", "request_id": f"review-why-port-cli-final-{round_number:02d}", "result": "failed",
                    "message": REAL[round_number - 1]})
        first = board.reconcile_verification_gaps(self.root)
        again = board.reconcile_verification_gaps(self.root)
        self.assertEqual((first["recorded"], again["recorded"]), (5, 0), "idempotent: nothing is duplicated")
        gap, = board.snapshot(self.root)["verification_gaps"].values()
        self.assertEqual((gap["state"], gap["rounds"], gap["total_rounds"]), ("diagnosing", 5, 5))
        self.assertEqual(self.open_cards(), [])

    def test_an_older_pass_never_settles_a_newer_untested_run_and_a_full_pass_does(self):
        qa = self.qa()
        with board.locked_state(self.root) as state:
            self.event(state, qa, "review-APP-final-1", "failed", OWNER_CAUSE, "2026-10-21T10:00:00+00:00")
            self.event(state, qa, "review-APP-final-2", "passed", "TASK DONE: YES - only the header was retested", "2026-10-21T11:00:00+00:00", test_scope="focused")
        for _ in range(2):
            board.reconcile_verification_gaps(self.root)
            gap, = board.snapshot(self.root)["verification_gaps"].values()
            self.assertEqual(gap["state"], "diagnosing", "a focused pass proves nothing about the untested part")
        with board.locked_state(self.root) as state:
            self.event(state, qa, "review-APP-final-3", "passed", "TASK DONE: YES", "2026-10-21T12:00:00+00:00")
        board.reconcile_verification_gaps(self.root)
        gap, = board.snapshot(self.root)["verification_gaps"].values()
        self.assertEqual(gap["state"], "closed")

    def test_an_owner_card_made_by_the_old_rule_is_resolved_and_the_gap_is_the_harness_s_to_settle(self):
        qa = self.qa()
        with board.locked_state(self.root) as state:
            request = {"id": "review-APP-final-1", "task": "APP", "phase": "final_acceptance", "subtask": "", "chunk": "", "structure_revision": 1}
            board._note_verification_gap(self.root, state, request, qa, OWNER_CAUSE)
            gap = next(iter(state["verification_gaps"].values()))
            gap["state"] = "owner_decision"       # what the previous version wrote
            card = {"id": "action-old", "title": "Your decision needed: APP could not be fully tested", "command": "", "why": "x", "task": "APP",
                    "for_agent": "", "kind": "gap", "gap_id": gap["id"], "agent_id": qa["id"], "status": "open", "recorded_at": board.now(), "outcome": "", "done_at": ""}
            state.setdefault("owner_actions", {})[card["id"]] = card
            gap["card_id"] = card["id"]
        board.reconcile_verification_gaps(self.root)
        gap, = board.snapshot(self.root)["verification_gaps"].values()
        self.assertEqual(gap["state"], "diagnosing")
        self.assertEqual(self.open_cards(), [])


for _cls in (GapTests, ReplayTests):
    for _name in [n for n in dir(base.ExecuteOnceTests) if n.startswith("test_") and n not in _cls.__dict__]:
        setattr(_cls, _name, None)


if __name__ == "__main__":
    unittest.main()
