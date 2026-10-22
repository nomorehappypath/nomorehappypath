# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The owner is told, in Mission Control, when a review ends NOT TESTED for a cause only he can settle (windows-walk item 12).

2026-10-21: five review rounds on the owner's Windows project ended `TASK DONE: NOT TESTED` for one reason nobody could change (the
access-denied case cannot occur on an administrator VM); Delivery resubmitted unchanged and the board raised no card, no question and
no message. Machine inputs removed: nothing is read from the machine (temporary project, real board).

Run:  PYTHONPATH=. python3 -m unittest tests.test_owner_told_when_stuck -v
"""
from __future__ import annotations

import json
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
        return [a for a in (self.state().get("owner_actions") or {}).values() if a.get("kind") == "gap" and a.get("status") == "open"]

    def test_an_owner_controlled_cause_raises_the_card_after_round_one_and_the_row_says_so(self):
        self.verdict(OWNER_CAUSE)
        gap, = self.gaps()
        self.assertEqual((gap["state"], gap["rounds"]), ("owner_decision", 1))
        self.assertEqual(board._not_tested_declaration(OWNER_CAUSE)[:12], "NOT TESTED —"[:12])
        card, = self.cards()
        self.assertIn("could not be fully tested", card["title"])
        for needle in ("waive this part", "give me a machine where it can happen", "stop this task", "Message the CTO", "cannot be waived"):
            self.assertIn(needle, card["why"])
        self.assertIn("cannot occur on an administrator VM", card["why"])
        home = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, home, True)
        registry.register(home, "alpha", self.root)
        row = project_manager.ProjectManager(home, board_port=0).projects_payload()["projects"][0]
        self.assertTrue(row["latest_progress"].startswith("Waiting for you: TASK-1 could not be fully tested"), row["latest_progress"])
        self.assertIn("waive this part, give me a machine where it can happen, or stop", row["latest_progress"])

    def test_the_reviewers_own_limit_is_diagnosed_and_a_repeat_stays_visible_but_never_becomes_the_owners(self):
        self.verdict(TOOL_CAUSE)
        gap, = self.gaps()
        self.assertEqual(gap["state"], "diagnosing")
        self.assertEqual(self.cards(), [], "no owner action for the reviewer's own tool limit in round one")
        note = board._verification_gap_note(self.state())
        self.assertIn("YOU ARE DIAGNOSING", note)
        # the same cause again, same scope: the owner is asked
        state = self.state()
        with board.locked_state(self.root) as live:
            qa = next(iter(live["agents"].values()))
            request = dict(next(iter(live["qa_requests"].values())), id="review-TASK-1-final-02")
            board._note_verification_gap(self.root, live, request, qa, TOOL_CAUSE)
        gap, = self.gaps()
        self.assertEqual((gap["state"], gap["rounds"]), ("diagnosing", 2), "two rounds of an agent limit are the CTO's to diagnose")
        self.assertEqual(self.cards(), [])
        self.assertIn("REPEATED NOT TESTED: round 2", board._verification_gap_note(self.state()))
        with board.locked_state(self.root) as live:
            qa = next(iter(live["agents"].values()))
            request = dict(next(iter(live["qa_requests"].values())), id="review-TASK-1-final-03")
            board._note_verification_gap(self.root, live, request, qa, "TASK DONE: NOT TESTED - and once more, in other words: the run did not finish.")
        gap, = self.gaps()
        self.assertEqual((gap["state"], gap["rounds"]), ("owner_decision", 3), "three rounds nobody settled: the owner is told, whatever the wording")
        self.assertIn("agents have not been able to settle it", self.cards()[0]["why"])

    def test_an_agent_limit_that_mentions_reproducing_is_not_the_owners(self):
        self.verdict("TASK DONE: NOT TESTED — the agent cannot reproduce this in its sandbox.")
        gap, = self.gaps()
        self.assertEqual(gap["state"], "diagnosing")

    def test_lookalikes_raise_nothing(self):
        for text in ("TASK DONE: YES — all of it ran", "TASK DONE: NO — a real defect", "VERDICT: FAIL\nTASK DONE: NO — see the old text: TASK DONE: NOT TESTED",
                     "no declaration at all, just a failure", "The earlier round said TASK DONE: NOT TESTED but this one ran everything."):
            self.assertEqual(board._not_tested_declaration(text), "", text)
        self.assertTrue(board._not_tested_declaration("TASK DONE: NOT TESTED — x"))
        self.assertTrue(board._not_tested_declaration("  task done: not reproducible here, x"))
        self.verdict("TASK DONE: NO — a real defect")
        self.assertEqual(self.gaps(), [])
        self.assertEqual(self.cards(), [])

    def test_the_unchanged_candidate_is_refused_until_something_changes(self):
        self.verdict(OWNER_CAUSE)
        with board.locked_state(self.root) as live:
            gap = next(iter(live["verification_gaps"].values()))
            gap["candidate"] = {"commit": "c1", "tree": "t1"}
            gap["scope_key"] = "TASK-1|final_acceptance|||0"
            with self.assertRaisesRegex(ValueError, "owner has been asked to decide"):
                board._gap_refusal(self.root, live, "TASK-1", "final_acceptance", "", "", {"tree_hash": "t1"})
            board._gap_refusal(self.root, live, "TASK-1", "final_acceptance", "", "", {"tree_hash": "t2"})      # a changed candidate is accepted
            gap["machine_revision"] = "9:9:9|qa"                                                              # a reconnected machine is a changed setting
            board._gap_refusal(self.root, live, "TASK-1", "final_acceptance", "", "", {"tree_hash": "t1"})
            board._gap_refusal(self.root, live, "OTHER", "final_acceptance", "", "", {"tree_hash": "t1"})      # another task is never blocked
            gap["machine_revision"] = board._current_machine_revision(self.root) + "|qa"
            gap["state"] = "closed"
            board._gap_refusal(self.root, live, "TASK-1", "final_acceptance", "", "", {"tree_hash": "t1"})      # the CTO's disposition lifts it

    def test_every_closing_path_closes_the_gap_and_the_card_and_keeps_the_history(self):
        for kind in ("task_cancelled", "git_acceptance_completed"):
            with self.subTest(kind=kind):
                self.setUp()
                self.verdict(OWNER_CAUSE)
                self.assertEqual(len(self.cards()), 1)
                with board.locked_state(self.root) as live:
                    board._event(live, kind, None, {"task": "TASK-1", "message": "x"})
                gap, = self.gaps()
                self.assertEqual((gap["state"], gap["rounds"]), ("closed", 1))
                self.assertEqual(self.cards(), [])

    def test_a_waiver_marks_receipt_only_the_ctos_disposition_closes_the_gap_and_a_new_run_starts_fresh(self):
        self.verdict(OWNER_CAUSE)
        with board.locked_state(self.root) as live:
            board._event(live, "deliverable_waived_by_owner", None, {"task": "TASK-1", "message": "waiver"})
        gap, = self.gaps()
        self.assertEqual(gap["state"], "owner_decision", "one waiver cannot be matched to this gap by text; nothing is dismissed")
        self.assertTrue(gap["owner_replied_at"])
        card, = self.cards()
        with board.locked_state(self.root) as live:
            board._resolve_owner_action(live, live["owner_actions"][card["id"]], "Resolved: the CTO recorded the waiver", "owner-action-done")
        gap, = self.gaps()
        self.assertEqual(gap["state"], "closed")
        with board.locked_state(self.root) as live:
            qa = next(iter(live["agents"].values()))
            request = dict(next(iter(live["qa_requests"].values())), id="review-TASK-1-final-09")
            board._note_verification_gap(self.root, live, request, qa, OWNER_CAUSE)
        gap, = self.gaps()
        self.assertEqual((gap["state"], gap["rounds"], gap["total_rounds"]), ("owner_decision", 1, 2))

    def test_the_owners_message_marks_the_answer_received_and_the_cto_is_told_to_apply_it(self):
        self.verdict(OWNER_CAUSE)
        with board.locked_state(self.root) as live:
            board._event(live, "owner_cto_message_received", None, {"task": "", "message": "waive the access-denied part"})
        gap, = self.gaps()
        self.assertTrue(gap["owner_replied_at"])
        self.assertIn("The owner has answered in Mission Control: apply it now", board._verification_gap_note(self.state()))
        cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        polled = board.poll(self.root, cto["id"])
        self.assertEqual([g["state"] for g in polled["verification_gaps"]], ["owner_decision"])

    def test_a_passing_review_of_the_scope_closes_it(self):
        self.verdict(OWNER_CAUSE)
        with board.locked_state(self.root) as live:
            request = next(iter(live["qa_requests"].values()))
            board._close_gaps_for(live, "TASK-1", "Resolved: the scope passed review", board._gap_scope_key(request))
        self.assertEqual(self.cards(), [])


# the five recorded verdict declarations of the owner's project (shortened), each worded differently by the Reviewer
REAL = [
    "TASK DONE: NOT TESTED - the denied-access path was not observable in the reviewer credentialed process. The independent run failed.",
    "TASK DONE: NOT TESTED - the denied-access owner path remains unverified because the reviewer credentialed process produced no output.",
    "TASK DONE: NOT TESTED - the denied-access owner path remains unverified because it is not reproducible on the available admin VM.",
    "TASK DONE: NOT TESTED - the access-denied owner path remains untested: the available Windows VM account is an administrator.",
    "TASK DONE: NOT TESTED - the owner-required access-denied message was not exercised: the only available Windows account is an administrator.",
]


class ReviewFindingsTests(base.ExecuteOnceTests):
    """Found by the independent review of 270136c: resume and two scopes of one task."""

    def qa(self):
        return board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")

    def event(self, state, qa, request_id, result, message, at, test_scope="full"):
        state["qa_requests"][request_id] = {"id": request_id, "task": "APP", "phase": "final_acceptance", "status": result, "cycle": 1, "subtask": "", "chunk": "",
                                            "developer_id": "d", "claimed_by": qa["id"], "review_wait_started_at": at, "requested_at": at, "test_scope": test_scope, "structure_revision": 1}
        event = board._event(state, "qa_result", qa, {"task": "APP", "request_id": request_id, "result": result, "message": message})
        event["at"] = at
        return event

    def test_resume_never_lets_an_older_pass_settle_a_newer_untested_run_and_repeats_are_stable(self):
        qa = self.qa()
        with board.locked_state(self.root) as state:
            self.event(state, qa, "review-APP-final-1", "failed", OWNER_CAUSE, "2026-10-21T10:00:00+00:00")
            self.event(state, qa, "review-APP-final-2", "passed", "TASK DONE: YES — ok", "2026-10-21T11:00:00+00:00")
            self.event(state, qa, "review-APP-final-3", "failed", OWNER_CAUSE, "2026-10-21T12:00:00+00:00")
        for _ in range(3):
            board.reconcile_verification_gaps(self.root)
            gap, = board.snapshot(self.root)["verification_gaps"].values()
            self.assertEqual((gap["state"], gap["request_id"], gap["rounds"], gap["total_rounds"]), ("owner_decision", "review-APP-final-3", 1, 2))
        cards = [a for a in board.snapshot(self.root)["owner_actions"].values() if a.get("kind") == "gap" and a.get("status") == "open"]
        self.assertEqual(len(cards), 1, "repeated resumes leave one open card")

    def test_a_focused_pass_replayed_on_resume_never_closes_the_gap_and_a_full_pass_does(self):
        qa = self.qa()
        with board.locked_state(self.root) as state:
            self.event(state, qa, "review-APP-final-1", "failed", OWNER_CAUSE, "2026-10-21T10:00:00+00:00")
            self.event(state, qa, "review-APP-final-2", "passed", "TASK DONE: YES - only the header was retested", "2026-10-21T11:00:00+00:00", test_scope="focused")
        for _ in range(3):
            board.reconcile_verification_gaps(self.root)
            gap, = board.snapshot(self.root)["verification_gaps"].values()
            self.assertEqual(gap["state"], "owner_decision", "a focused pass proves nothing about the untested part")
            self.assertEqual(len([a for a in board.snapshot(self.root)["owner_actions"].values() if a.get("kind") == "gap" and a.get("status") == "open"]), 1)
        with board.locked_state(self.root) as state:
            self.event(state, qa, "review-APP-final-3", "passed", "TASK DONE: YES", "2026-10-21T12:00:00+00:00")
        board.reconcile_verification_gaps(self.root)
        gap, = board.snapshot(self.root)["verification_gaps"].values()
        self.assertEqual(gap["state"], "closed")

    def test_a_replacement_reviewer_may_retry_and_the_same_reviewer_may_not(self):
        qa = self.qa()
        gap_message = "TASK DONE: NOT TESTED - the Reviewer's sandbox timed out"
        with board.locked_state(self.root) as state:
            for number in range(1, 4):
                request = {"id": f"review-APP-final-{number}", "task": "APP", "phase": "final_acceptance", "subtask": "", "chunk": "", "structure_revision": 1,
                           "reviewed_tree_hash": "tree-1", "reviewed_commit": "c1"}
                board._note_verification_gap(self.root, state, request, qa, gap_message)
            gap, = state["verification_gaps"].values()
            self.assertEqual((gap["state"], gap["reviewers"]), ("owner_decision", [qa["id"]]))
            artifact = {"tree_hash": "tree-1"}
            with self.assertRaisesRegex(ValueError, "repeat the same verdict"):
                board._gap_refusal(self.root, state, "APP", "final_acceptance", "", "", artifact)
            qa["active"] = False
            state["agents"][qa["id"]]["active"] = False
        replacement = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        with board.locked_state(self.root) as state:
            board._gap_refusal(self.root, state, "APP", "final_acceptance", "", "", {"tree_hash": "tree-1"})   # a different Reviewer: allowed
            request = {"id": "review-APP-final-4", "task": "APP", "phase": "final_acceptance", "subtask": "", "chunk": "", "structure_revision": 1,
                       "reviewed_tree_hash": "tree-1", "reviewed_commit": "c1"}
            board._note_verification_gap(self.root, state, request, state["agents"][replacement["id"]], gap_message)
            gap, = state["verification_gaps"].values()
            self.assertEqual(gap["reviewers"], [qa["id"], replacement["id"]])
            self.assertEqual(gap["rounds"], 4, "the history and visibility are kept")
            with self.assertRaisesRegex(ValueError, "repeat the same verdict"):
                board._gap_refusal(self.root, state, "APP", "final_acceptance", "", "", {"tree_hash": "tree-1"})   # the replacement failed too

    def test_two_scopes_of_one_task_keep_their_own_gaps_cards_and_closure(self):
        qa = self.qa()
        with board.locked_state(self.root) as state:
            for chunk in ("page", "api"):
                board._note_verification_gap(self.root, state, {"id": f"review-APP-{chunk}-01", "task": "APP", "phase": "chunk", "chunk": chunk, "structure_revision": 1},
                                             qa, OWNER_CAUSE)
        state = board.snapshot(self.root)
        cards = [a for a in state["owner_actions"].values() if a.get("kind") == "gap" and a.get("status") == "open"]
        self.assertEqual(len(cards), 2, "one card per gap, never merged by the generic same-task deduplication")
        self.assertEqual({c["gap_id"] for c in cards}, {g["id"] for g in state["verification_gaps"].values()})
        with board.locked_state(self.root) as live:
            board._close_gaps_for(live, "APP", "Resolved: the scope passed review", "APP|chunk|page|1")  # wrong shape on purpose: nothing closes
            board._close_gaps_for(live, "APP", "Resolved: the scope passed review", board._gap_scope_key({"task": "APP", "phase": "chunk", "chunk": "page", "structure_revision": 1}))
        state = board.snapshot(self.root)
        by_scope = {g["scope"]["chunk"]: g["state"] for g in state["verification_gaps"].values()}
        self.assertEqual(by_scope, {"page": "closed", "api": "owner_decision"})
        open_cards = [a for a in state["owner_actions"].values() if a.get("kind") == "gap" and a.get("status") == "open"]
        self.assertEqual([c["gap_id"] for c in open_cards], [next(g["id"] for g in state["verification_gaps"].values() if g["scope"]["chunk"] == "api")])

    def test_the_card_footer_is_truthful_and_receipt_reaches_the_card(self):
        qa = self.qa()
        with board.locked_state(self.root) as state:
            board._note_verification_gap(self.root, state, {"id": "review-APP-final-01", "task": "APP", "phase": "final_acceptance", "structure_revision": 1}, qa, OWNER_CAUSE)
            card = next(a for a in state["owner_actions"].values() if a.get("kind") == "gap")
            self.assertIn("it does not expire", board.owner_action_clears_when(card))
            board._event(state, "owner_cto_message_received", None, {"task": "", "message": "waive it"})
            card = state["owner_actions"][card["id"]]
        self.assertTrue(card["owner_replied_at"])
        self.assertIn("Your answer was received; the CTO is applying it", board.owner_action_clears_when(card))
        self.assertNotIn("Expires", board.owner_action_clears_when(card))


class WhyWindowsReplayTests(base.ExecuteOnceTests):
    """Today's board shape: five NOT TESTED rounds, no card. Replayed from the recorded events."""

    def test_the_card_is_raised_from_the_recorded_rounds_once_and_never_twice(self):
        qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        with board.locked_state(self.root) as state:
            for round_number in range(1, 6):
                board._event(state, "qa_result", qa, {
                    "task": "why-port-cli", "request_id": f"review-why-port-cli-final-{round_number:02d}", "result": "failed",
                    "message": REAL[round_number - 1]})
        first = board.reconcile_verification_gaps(self.root)
        again = board.reconcile_verification_gaps(self.root)
        self.assertEqual(first["recorded"], 5)
        self.assertEqual(again["recorded"], 0, "idempotent: nothing is duplicated")
        state = board.snapshot(self.root)
        gap, = state["verification_gaps"].values()
        self.assertEqual((gap["state"], gap["rounds"], gap["total_rounds"]), ("owner_decision", 5, 5))
        cards = [a for a in state["owner_actions"].values() if a.get("kind") == "gap" and a.get("status") == "open"]
        self.assertEqual(len(cards), 1, json.dumps(cards))
        self.assertIn("This is round 5", cards[0]["why"])

    def test_after_round_one_the_project_says_not_fully_tested_and_the_card_comes_with_the_owners_cause_or_round_three(self):
        qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        for number, expected in ((1, "diagnosing"), (2, "diagnosing"), (3, "owner_decision")):
            with board.locked_state(self.root) as state:
                board._event(state, "qa_result", qa, {"task": "why-port-cli", "request_id": f"review-why-port-cli-final-{number:02d}", "result": "failed", "message": REAL[number - 1]})
            board.reconcile_verification_gaps(self.root)
            gap, = board.snapshot(self.root)["verification_gaps"].values()
            self.assertEqual((gap["rounds"], gap["state"]), (number, expected))
        home = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, home, True)
        registry.register(home, "alpha", self.root)
        self.assertIn("Waiting for you: why-port-cli could not be fully tested", project_manager.ProjectManager(home, board_port=0).projects_payload()["projects"][0]["latest_progress"])


for _cls in (GapTests, ReviewFindingsTests, WhyWindowsReplayTests):
    for _name in [n for n in dir(base.ExecuteOnceTests) if n.startswith("test_") and n not in _cls.__dict__]:
        setattr(_cls, _name, None)


if __name__ == "__main__":
    unittest.main()
