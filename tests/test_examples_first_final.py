# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Examples first, the end of the build (task 2026-11-24_tdd-examples-first, Changes 4 and 5).

At the final review every requirement of the accepted contract is tested for real (a counted stored check that passed on the
reviewed commit, or a Challenge Ledger row naming its number) or said plainly not to be (`Not run for real: requirement N`);
with no list prepared before the build the final Reviewer files it first; the result of the owner's scenario is checked
against the list he was shown; and the real run is named for every app. A refusal here is a call to correct: the review
stays claimed, no round fails and no owner card is raised.

Run:  PYTHONPATH=. python3 -m unittest tests.test_examples_first_final -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, environment_check, examples_first
from tests.test_contract_first_checks import HEADER, TASK, TEXT, FinalReviewFlowTests, write_json
from tests.test_examples_first import RESULT, RUNS_PROGRAM, check, criterion, submission

LOUD = "Running it with the argument loud prints HELLO in capital letters."
REQUIREMENT_HEADER = HEADER.replace("| QA result |\n", "| QA result | Requirement |\n").replace("|---|---|---|---|---|---|\n", "|---|---|---|---|---|---|---|\n")
DONE = "TASK DONE: YES - the greeting is printed: the stored check and my own scenario hold"
ITEM_1 = "\nResult item 1: MET - the output is the single word hello"


def ledger_naming(*numbers: int) -> str:
    """A Challenge Ledger whose one row says, in its Requirement column, which accepted requirements it tests."""
    named = ", ".join(str(number) for number in numbers)
    return REQUIREMENT_HEADER + ("| S-101 | The independent check confirms the behavior through a distinct test scope. | "
                                 f"`python3 -m unittest test_smoke -k passes` | Holds | PASS: executed | PASS | {named} |\n")


class PureRulesTests(unittest.TestCase):
    def test_a_ledger_row_keeps_the_requirement_numbers_it_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "challenge.md"
            path.write_text(ledger_naming(2, 5), encoding="utf-8")
            valid, problems, rows = contract.scenario_simulations(path)
            self.assertTrue(valid, problems)
            self.assertEqual(rows[0]["requirements"], [2, 5])
            self.assertEqual(examples_first.ledger_requirements(path), {2, 5})
            path.write_text(HEADER + "| S-101 | The independent check confirms the behavior through a distinct test scope. | "
                            "`python3 -m unittest test_smoke` | Holds | PASS: executed | PASS |\n", encoding="utf-8")
            self.assertEqual(contract.scenario_simulations(path)[2][0]["requirements"], [], "an older ledger without the column is unchanged")

    def test_the_reviewers_own_lines_are_read_whatever_their_bullet(self):
        summary = ("TASK DONE: YES\n- Not run for real: requirement 4 - the browser was refused here\n"
                   "Not run for real: requirements 6, 7 and 9 — no test machine\n* Result item 1: MET - first paragraph\nResult item 2: NOT MET - no dates")
        self.assertEqual(examples_first.not_run_requirements(summary), {4: "the browser was refused here", 6: "no test machine", 7: "no test machine", 9: "no test machine"})
        self.assertEqual(examples_first.result_items(summary), {1: True, 2: False})

    def test_a_requirement_is_done_by_a_passed_counted_check_a_named_row_or_a_not_run_line_and_by_nothing_else(self):
        counted = {"n": 1, "examples": ["e"], "checks": [{"coverage": "checked", "runs": "app", "example": 1, "covers": [4]}]}
        partly = {"n": 2, "examples": ["e"], "checks": [{"coverage": "partly"}]}
        none = {"n": 3, "examples": ["e"], "checks": []}
        covered = {"n": 4, "examples": ["e"], "checks": []}
        silent = {"n": 5, "examples": ["e"], "checks": []}
        criteria = [counted, partly, none, covered, silent]
        proof = examples_first.proof_by_requirement(criteria, {"1": "passed", "2": "passed"}, {2}, "Not run for real: requirement 3 - no machine")
        self.assertEqual(proof, {"1": examples_first.STORED, "2": examples_first.ROW, "3": "not tested for real: no machine",
                                 "4": examples_first.STORED, "5": ""})
        failed = examples_first.proof_by_requirement(criteria, {"1": "failed"}, set(), "")
        self.assertEqual(failed["1"], "", "a counted check that did not pass on this version proves nothing")

    def test_the_real_run_is_named_for_every_app_and_an_older_list_keeps_its_rules(self):
        state: dict = {"task_kinds": {"T": "development"}}
        no_ai = {"ai_tools": [], "programs": [{"name": "python3"}]}
        self.assertEqual(environment_check.needs_problem(state, "T", no_ai), "", "a list filed before the change, or by an older caller, is unchanged")
        self.assertFalse(environment_check.real_run_required(state, "T", no_ai))
        problem = environment_check.needs_problem(state, "T", no_ai, every_app=True)
        self.assertIn("this task builds or changes a program: the setup list must name real_run, real_scenario, real_output", problem)
        named = {**no_ai, "real_run": "python3 run_it.py", "real_scenario": "A city in, its temperature out", "real_output": "out/result.txt"}
        self.assertEqual(environment_check.needs_problem(state, "T", named, every_app=True), "")
        self.assertTrue(environment_check.real_run_required(state, "T", named))
        self.assertEqual(environment_check.needs_problem({"task_kinds": {"T": "analysis"}}, "T", no_ai, every_app=True), "")

    def test_a_real_run_with_no_ai_tool_passes_on_its_fresh_result_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            item = environment_check.real_run("mkdir -p out && echo 21C > out/result.txt", [], temporary, dict(os.environ), lambda text: text,
                                              real_output="out/result.txt")
            self.assertTrue(item["ok"], item)
            self.assertIn("ran through to its result", item["detail"])
            stale = environment_check.real_run("true", [], temporary, dict(os.environ), lambda text: text, real_output="out/result.txt")
            self.assertFalse(stale["ok"], "a result left by an earlier run never counts")


class FinalRuleTests(unittest.TestCase):
    """The Reviewer's own qa-result command (requirement_proof_required=True) on a real committed candidate."""

    setUp = FinalReviewFlowTests.setUp
    request = FinalReviewFlowTests.request

    def confirm(self, value: dict | None) -> None:
        """Propose, optionally record the Reviewer's early look, go ahead, confirm and plan."""
        board.record_requirement_proposal(self.root, self.dev["id"], TEXT)
        if value is not None:
            board.record_contract_check_submission(self.root, self.qa["id"], TASK, 1, write_json(self.base, "sub.json", value))
            token = board.snapshot(self.root)["requirement_proposals"][TASK]["contract_check"]["token"]
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=token)
        else:
            reading = board.snapshot(self.root)["requirement_proposals"][TASK]["contract_check"]
            self.assertEqual(reading["status"], "reading")
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=reading.get("token", ""))   # the owner goes ahead before any early look
        board.record_requirement_confirmation(self.root, self.dev["id"], TEXT)
        board.define_delivery_plan(self.root, self.dev["id"], "atomic", "One cohesive task")

    def claimed(self, ledger_text: str) -> str:
        """Request the final review and claim it with the given Challenge Ledger; return the request id."""
        request = self.request()
        ledger_path = self.base / "challenge-for-review.md"
        ledger_path.write_text(ledger_text, encoding="utf-8")
        board.claim_qa(self.root, self.qa["id"], request["id"], str(ledger_path))
        board.execute_challenge(self.root, self.qa["id"], request["id"])
        return request["id"]

    def verdict(self, request_id: str, summary: str) -> dict:
        evidence = self.base / "evidence.txt"
        evidence.write_text("command: python3 -m unittest\nresult: PASS\n")
        return board.qa_result(self.root, self.qa["id"], request_id, "passed", summary, str(evidence), requirement_proof_required=True)

    def two_requirements(self) -> dict:
        return submission(criterion([check({"test_hello.py": RUNS_PROGRAM}, runs="python3 hello.py", example=1)]),
                          criterion(text="The greeting can be made loud", examples=[LOUD]))

    def block(self) -> dict:
        return board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]

    def still_claimed_and_nothing_else_happened(self, request_id: str) -> None:
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"][request_id]["status"], "claimed", "a refused call is not a failed review")
        self.assertFalse([item for item in (state.get("owner_actions") or {}).values() if item.get("status") == "open"], "no owner card")

    def test_a_requirement_with_no_real_test_cannot_pass_silently(self):
        self.confirm(self.two_requirements())
        request_id = self.claimed(HEADER + "| S-101 | The independent check confirms the behavior through a distinct test scope. | "
                                  "`python3 -m unittest test_smoke -k passes` | Holds | PASS: executed | PASS |\n")
        brief = board.snapshot(self.root)["qa_requests"][request_id]["review_brief"]["final_stage"]["examples_first"]
        self.assertEqual(brief["need_a_real_test_in_this_review"], [2])
        self.assertEqual(brief["requirements"][1]["examples"], [LOUD])
        self.assertEqual(brief["result_must_contain"], RESULT)
        self.assertTrue(brief["examples_approved_before_the_build"])
        pass
        self.still_claimed_and_nothing_else_happened(request_id)
        self.verdict(request_id, DONE + ITEM_1 + "\nNot run for real: requirement 2 - this machine has no loud mode to try")
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request_id]["status"], "passed")
        self.assertEqual(self.block()["final_proof"], {"1": examples_first.STORED, "2": "not tested for real: this machine has no loud mode to try"})

    def test_a_row_that_names_the_requirement_is_its_real_test(self):
        self.confirm(self.two_requirements())
        request_id = self.claimed(ledger_naming(2))
        self.verdict(request_id, DONE + ITEM_1)
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request_id]["status"], "passed")
        self.assertEqual(self.block()["final_proof"], {"1": examples_first.STORED, "2": examples_first.ROW})

    def test_a_requirement_the_early_list_left_out_is_added_and_then_held_to_the_rule(self):
        self.confirm(submission(criterion([check({"test_hello.py": RUNS_PROGRAM}, runs="python3 hello.py", example=1)])))
        request_id = self.claimed(ledger_naming(1))
        added = write_json(self.base, "added.json", {"text": "The greeting can be made loud", "plain": "You can ask for a loud greeting.", "examples": [LOUD], "checks": []})
        wrong = write_json(self.base, "wrong.json", {"text": "A third thing", "plain": "You get a third thing as well.", "examples": [LOUD], "checks": []})
        with self.assertRaisesRegex(ValueError, "as its next number"):
            board.record_contract_check_correction(self.root, self.qa["id"], TASK, 5, "a requirement the early list left out", wrong)
        result = board.record_contract_check_correction(self.root, self.qa["id"], TASK, 2, "a requirement the early list left out", added)
        self.assertEqual(result, {"criterion": 2, "added": True, "checks": 0})
        self.assertEqual(self.block()["labels"][1]["examples"], [LOUD])
        pass
        self.verdict(request_id, DONE + ITEM_1 + "\nNot run for real: requirement 2 - added at the final review, no loud mode here")
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request_id]["status"], "passed")

    def test_the_result_is_checked_against_the_list_the_owner_was_shown(self):
        self.confirm(submission(criterion([check({"test_hello.py": RUNS_PROGRAM}, runs="python3 hello.py", example=1)]),
                                result_must_contain=["The word hello on its own line", "Nothing else printed"]))
        request_id = self.claimed(ledger_naming(1))
        pass
        with self.assertRaisesRegex(ValueError, "you wrote NOT MET for result item 2.*record FAIL"):
            self.verdict(request_id, DONE + ITEM_1 + "\nResult item 2: NOT MET - a second line is printed")
        self.still_claimed_and_nothing_else_happened(request_id)
        self.verdict(request_id, DONE + ITEM_1 + "\nResult item 2: MET - one line only")
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request_id]["status"], "passed")

    def test_a_contract_confirmed_before_these_rules_keeps_its_own(self):
        self.confirm(self.two_requirements())
        with board.locked_state(self.root) as state:
            state["requirement_confirmations"][TASK]["contract_check"].pop("rules")      # what an older board stored
        request_id = self.claimed(HEADER + "| S-101 | The independent check confirms the behavior through a distinct test scope. | "
                                  "`python3 -m unittest test_smoke -k passes` | Holds | PASS: executed | PASS |\n")
        self.verdict(request_id, DONE)
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request_id]["status"], "passed")
        self.assertNotIn("final_proof", self.block())


if __name__ == "__main__":
    unittest.main()
