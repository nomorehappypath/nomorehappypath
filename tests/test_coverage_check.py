# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The coverage check on the Reviewer's early look: every requirement is covered, or the owner is told plainly where it is not.

Run:  PYTHONPATH=. python3 -m unittest tests.test_coverage_check -v
"""
from __future__ import annotations

import copy
import unittest

from harness import board, contract_check, control
from tests.test_contract_first_checks import TASK, TEST_FILE, TEXT, _Fixture, ledger, write_json

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
