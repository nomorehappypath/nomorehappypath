# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The coverage check on the Reviewer's early look: every requirement is covered, or the owner is told plainly where it is not.

Run:  PYTHONPATH=. python3 -m unittest tests.test_coverage_check -v
"""
from __future__ import annotations

import copy
import unittest

from harness import board, contract_check, control
from tests.test_contract_first_checks import QUESTION, TASK, TEST_FILE, TEXT, _Fixture, ledger, write_json

CHECK = {"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE}}


def criteria(*specs):
    """specs: (checks?, failure_paths?) per requirement."""
    out = []
    for n, (has_check, paths) in enumerate(specs, 1):
        item = {"text": f"Requirement number {n} holds", "plain": "This requirement protects what you asked for."}
        if has_check:
            item["checks"] = [copy.deepcopy(CHECK)]
        else:
            item["why_not_checked"] = "nothing to run"
        if paths is True:
            item["failure_paths"] = ["a missing name is refused"]
        elif paths:
            item["no_failure_paths_because"] = "it has no failure case"
        else:
            item["failure_paths"] = []        # said, and empty: the normal case only
        out.append(item)
    return {"criteria": out, "questions": []}


class CoverageTests(_Fixture):
    def start(self):
        agent = self.delivery()
        session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        control.take_instructions(self.root, session["id"])
        return session, reviewer

    def asked(self, session):
        return [item["text"] for item in control.take_instructions(self.root, session["id"]) if "CONTRACT CHECK COVERAGE" in item["text"]]

    def lines(self):
        return [item["coverage_line"] for item in self.proposal()["contract_check"]["criteria"]]

    def test_mapping_is_recorded_and_all_covered_asks_nothing(self):
        session, reviewer = self.start()
        value = criteria((True, True), (True, "no_case"))
        value["criteria"][0]["checks"][0]["covers"] = [2]
        self.submit(reviewer, value)
        self.assertEqual(self.lines(), ["covered", "covered"])
        stored = self.proposal()["contract_check"]["criteria"]
        self.assertEqual((stored[0]["failure_paths"], stored[1]["also_by"]), (["a missing name is refused"], 1))
        self.assertEqual(self.asked(session), [])

    def test_requirement_without_a_check_is_a_gap_asked_once_and_go_ahead_is_not_held(self):
        session, reviewer = self.start()
        self.submit(reviewer, criteria((True, True), (False, False)))
        self.assertEqual(self.lines(), ["covered", contract_check.GAP_NO_TEST])
        asked = self.asked(session)
        self.assertEqual(len(asked), 1)
        self.assertIn("Requirement number 2", asked[0])
        self.assertEqual(self.proposal()["contract_check"]["status"], "ready")
        # the one answer closes the gap through the correction path
        fix = write_json(self.base, "fix.json", {"checks": [copy.deepcopy(CHECK)], "failure_paths": ["a bad input is refused"]})
        board.record_contract_check_correction(self.root, reviewer["id"], TASK, 2, "closing a coverage gap", fix)
        self.assertEqual(self.lines(), ["covered", "covered"])
        with self.assertRaisesRegex(ValueError, "at least one check|no stored check"):
            board.record_contract_check_correction(self.root, reviewer["id"], TASK, 2, "closing a coverage gap again", fix)
        self.assertEqual(self.asked(session), [], "never a second request")
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])
        self.assertEqual(self.proposal()["status"], "accepted")

    def test_go_ahead_with_an_open_gap_is_accepted(self):
        _session, reviewer = self.start()
        self.submit(reviewer, criteria((True, False)))
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])
        self.assertEqual(self.proposal()["status"], "accepted")
        confirmed = board.snapshot(self.root)["requirement_proposals"][TASK]["contract_check"]["criteria"][0]
        self.assertEqual(confirmed["coverage_line"], contract_check.GAP_NORMAL_ONLY)

    def gap_then_go_ahead(self):
        """Requirement 2 has no check, the Reviewer was asked once, and the owner says Go ahead before it answers."""
        agent = self.delivery()
        session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        control.take_instructions(self.root, session["id"])
        self.submit(reviewer, criteria((True, True), (False, False)))
        self.assertEqual(len(self.asked(session)), 1)
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])
        return agent, reviewer, write_json(self.base, "late.json", {"checks": [copy.deepcopy(CHECK)], "failure_paths": ["a bad input is refused"]})

    def confirmed_block(self):
        return board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]

    def test_a_gap_answered_after_go_ahead_and_before_the_contract_is_confirmed_is_taken(self):
        """Second stranger run 2026-11-05: Go ahead came a minute before the Reviewer's answer; every answer was then refused
        ("no stored check to correct") and 5 of 11 requirements were built with no test written in advance."""
        agent, reviewer, fix = self.gap_then_go_ahead()
        board.record_contract_check_correction(self.root, reviewer["id"], TASK, 2, "closing a coverage gap", fix)
        self.assertEqual(self.lines(), ["covered", "covered"])
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        self.assertEqual(sorted({c["criterion_index"] for c in self.confirmed_block()["checks"]}), [1, 2], "the late check is in the locked contract")

    def test_a_gap_answered_after_the_contract_is_confirmed_adds_the_first_checks(self):
        agent, reviewer, fix = self.gap_then_go_ahead()
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        before = self.confirmed_block()
        self.assertEqual([c["criterion_index"] for c in before["checks"]], [1])
        result = board.record_contract_check_correction(self.root, reviewer["id"], TASK, 2, "closing a coverage gap", fix)
        self.assertEqual(result["checks"], 1)
        after = self.confirmed_block()
        self.assertEqual(sorted({c["criterion_index"] for c in after["checks"]}), [1, 2])
        self.assertEqual(next(c for c in after["checks"] if c["criterion_index"] == 2)["criterion"], "Requirement number 2 holds")
        self.assertNotEqual(after["checks_sha256"], before["checks_sha256"], "the lock covers the new check")
        self.assertEqual([label["coverage"] for label in after["labels"]], ["covered", "covered"])
        added = [e for e in board.snapshot(self.root)["events"] if e["kind"] == "contract_check_corrected"]
        self.assertIn("added the first checks for a requirement that had none", added[-1]["message"])
        with self.assertRaisesRegex(ValueError, "no stored check to correct"):
            board.record_contract_check_correction(self.root, reviewer["id"], TASK, 9, "not a requirement of this contract", fix)

    def test_a_gap_answered_after_a_go_ahead_that_also_answered_a_question_is_taken(self):
        """Codex R1-1: answering the Reviewer's question puts the check in the state "answered"; the late coverage answer
        was still refused there and the contract was confirmed without the check."""
        agent = self.delivery()
        session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        control.take_instructions(self.root, session["id"])
        value = criteria((True, True), (False, False))
        value["questions"] = [QUESTION]
        self.submit(reviewer, value)
        token = self.proposal()["contract_check"]["token"]
        board.record_requirements_decision(self.root, TASK, "answer", version=1, token=token, answers=[{"question": 1, "choice": 1, "other": ""}])
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=self.proposal()["version"], token=self.proposal()["contract_check"]["token"])
        self.assertEqual((self.proposal()["status"], self.proposal()["contract_check"]["status"]), ("accepted", "answered"))
        fix = write_json(self.base, "late.json", {"checks": [copy.deepcopy(CHECK)], "failure_paths": ["a bad input is refused"]})
        board.record_contract_check_correction(self.root, reviewer["id"], TASK, 2, "closing a coverage gap", fix)
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        # requirement 1's own check went stale with the answered question (existing rule); requirement 2's late check must be there
        self.assertIn(2, {c["criterion_index"] for c in self.confirmed_block()["checks"]}, "the late check survives confirmation")

    def test_a_contract_confirmed_with_no_check_at_all_can_receive_its_first_one(self):
        """Codex R1-2: with zero checks the contract's fingerprint is empty; that must not read as "not confirmed"."""
        agent = self.delivery()
        session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        control.take_instructions(self.root, session["id"])
        self.submit(reviewer, criteria((False, False)))
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        self.assertEqual((self.confirmed_block().get("checks") or [], self.confirmed_block().get("checks_sha256") or ""), ([], ""))
        fix = write_json(self.base, "first.json", {"checks": [copy.deepcopy(CHECK)], "failure_paths": ["a bad input is refused"]})
        result = board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "closing a coverage gap", fix)
        self.assertEqual(result["checks"], 1)
        after = self.confirmed_block()
        self.assertEqual([c["criterion_index"] for c in after["checks"]], [1])
        self.assertTrue(after["checks_sha256"], "the first check is locked")
        with self.assertRaisesRegex(ValueError, "no stored check to correct"):
            board.record_contract_check_correction(self.root, reviewer["id"], TASK, 7, "not a requirement", fix)

    def test_happy_path_only_is_a_gap(self):
        session, reviewer = self.start()
        self.submit(reviewer, criteria((True, False)))
        self.assertEqual(self.lines(), [contract_check.GAP_NORMAL_ONLY])
        self.assertEqual(len(self.asked(session)), 1)

    def test_older_submission_without_mapping_loads_as_not_mapped_and_asks_nothing(self):
        session, reviewer = self.start()
        value = criteria((True, False), (False, False))
        self.submit(reviewer, value)
        self.asked(session)
        # what an older board stored: no mapping keys at all (loaded, never re-recorded)
        with board.locked_state(self.root) as state:
            for item in state["requirement_proposals"][TASK]["contract_check"]["criteria"]:
                for key in ("failure_paths", "no_failure_paths_because", "mapped", "new", "also_by", "coverage_line"):
                    item.pop(key, None)
        labels = contract_check.labels(self.proposal()["contract_check"]["criteria"])
        self.assertEqual({item["coverage"] for item in labels}, {"not mapped"})

    def test_a_new_submission_that_says_nothing_about_coverage_is_a_gap_asked_once(self):
        session, reviewer = self.start()
        plain = {"criteria": [{"text": "Requirement number 1 holds", "plain": "This requirement protects what you asked for.", "checks": [copy.deepcopy(CHECK)]}], "questions": []}
        self.submit(reviewer, plain)
        self.assertEqual(self.lines(), [contract_check.NOT_MAPPED_NEW])
        self.assertEqual(len(self.asked(session)), 1)
        self.assertEqual(self.proposal()["contract_check"]["status"], "ready")

    def test_owner_questions_do_not_stop_the_coverage_request_or_hold_go_ahead(self):
        session, reviewer = self.start()
        value = criteria((True, False))
        value["questions"] = [{"text": "Which greeting should it print?", "options": ["hello", "hi"], "recommended": 0, "affects": [1]}]
        self.submit(reviewer, value)
        self.assertEqual(self.proposal()["contract_check"]["status"], "questions")
        self.assertEqual(len(self.asked(session)), 1)
        fix = write_json(self.base, "fix.json", {"checks": [], "failure_paths": ["a bad input is refused"]})
        board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "closing a coverage gap", fix)
        self.assertEqual(self.lines(), ["covered"])
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])
        self.assertEqual(self.proposal()["status"], "accepted")


    def test_a_correction_that_adds_a_mapping_recomputes_the_other_requirement(self):
        session, reviewer = self.start()
        self.submit(reviewer, criteria((True, True), (False, True), (False, True)))
        self.assertEqual(self.lines(), ["covered", contract_check.GAP_NO_TEST, contract_check.GAP_NO_TEST])
        self.asked(session)
        extra = copy.deepcopy(CHECK)
        extra["covers"] = [3]
        fix = write_json(self.base, "fix.json", {"checks": [extra]})
        board.record_contract_check_correction(self.root, reviewer["id"], TASK, 2, "closing a coverage gap", fix)
        self.assertEqual(self.lines(), ["covered", "covered", "covered"], "requirement 3 is covered by the check requirement 2 now carries")
        self.assertEqual(self.proposal()["contract_check"]["criteria"][1]["checks"][0]["covers"], [3], "the mapping is kept in storage")

    def test_removing_the_only_check_that_covers_a_requirement_makes_it_a_gap_again(self):
        stored = self.submit_and_load(criteria((True, True), (False, True)), covers=True)
        self.assertEqual([item["coverage_line"] for item in stored], ["covered", "covered"])
        stored[0]["checks"] = [{"coverage": "checked", "file": "x", "sha256": "y"}]          # the check is replaced by one that no longer covers 2
        contract_check.apply_coverage(stored)
        self.assertEqual([item["coverage_line"] for item in stored], ["covered", contract_check.GAP_NO_TEST])
        stored[0]["stale"] = True
        contract_check.apply_coverage(stored)
        self.assertEqual(stored[0]["coverage_line"], contract_check.GAP_NO_TEST, "a set-aside check covers nothing")

    def submit_and_load(self, value, covers=False):
        _session, reviewer = self.start()
        if covers:
            value["criteria"][0]["checks"][0]["covers"] = [2]
        self.submit(reviewer, value)
        return self.proposal()["contract_check"]["criteria"]

    def test_no_keys_submission_then_a_correction_with_the_mapping_is_covered(self):
        session, reviewer = self.start()
        plain = {"criteria": [{"text": "Requirement number 1 holds", "plain": "This requirement protects what you asked for.", "checks": [copy.deepcopy(CHECK)]}], "questions": []}
        self.submit(reviewer, plain)
        self.assertEqual(self.lines(), [contract_check.NOT_MAPPED_NEW])
        token = self.proposal()["contract_check"]["token"]
        fix = write_json(self.base, "fix.json", {"checks": [], "failure_paths": ["a bad input is refused"]})
        board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "closing a coverage gap", fix)
        self.assertEqual(self.lines(), ["covered"])
        self.assertEqual(contract_check.labels(self.proposal()["contract_check"]["criteria"])[0]["coverage"], "covered")
        self.assertEqual(self.proposal()["contract_check"]["token"], token, "the owner's Go ahead is not held")


if __name__ == "__main__":
    unittest.main()
