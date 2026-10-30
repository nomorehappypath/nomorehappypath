# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Code-quality notes in the review, and non-functional requirements in the contract (task 2026-10-26_code-quality-and-nfr).

Real board calls (the frozen-scope review fixture and the red-proof project fixture): the Reviewer's `--code-quality` lines are kept as
notes and never change what a verdict means; a requirement marked non-functional is stored with its kind and goes through the same
coverage line and red proof as any other requirement.

Run:  PYTHONPATH=. python3 -m unittest tests.test_code_quality_and_nfr -v
"""
from __future__ import annotations

import contextlib
import io
import json
import unittest

from harness import board, board_mcp, code_quality, contract_check, red_proof
from tests import test_frozen_review_scope as frozen
from tests import test_repair_test_integrity as integrity
from tests.test_contract_first_checks import TEXT, write_json

TASK = integrity.TASK


class CodeQualityNotes(frozen.Rounds):
    # the inherited review tests are not repeated in this module
    for _name in dir(frozen.Rounds):
        if _name.startswith("test"):
            locals()[_name] = None
    del _name

    def cli(self, request, qa, result, summary, *extra):
        with contextlib.redirect_stdout(io.StringIO()):
            return board.main(["--root", str(self.root), "qa-result", "--agent", qa["id"], "--request", request["id"], "--result", result,
                               "--summary", summary, "--evidence", self.proof(), *extra])

    def task_of(self, request):
        return self.state()["qa_requests"][request["id"]]["task"]

    def test_the_flag_is_optional_on_the_command_and_in_the_agent_tool(self):
        parser = board.build_parser()
        base = ["qa-result", "--agent", "a", "--request", "r", "--result", "passed", "--summary", "s"]
        self.assertEqual(parser.parse_args(base).code_quality, [])
        self.assertEqual(parser.parse_args(base + ["--code-quality", "a.py: x", "--code-quality", "b.py: y"]).code_quality, ["a.py: x", "b.py: y"])
        schema = next(item for item in board_mcp.tool_definitions("qa") if item["name"] == "qa-result")["inputSchema"]
        self.assertIn("code_quality", schema["properties"])
        self.assertNotIn("code_quality", schema["required"])

    def test_notes_on_a_round_one_fail_are_stored_and_the_blockers_are_exactly_what_they_were(self):
        first = self.request()
        qa = self.claim(first)
        code = self.cli(first, qa, "failed", "TASK DONE: NO — totals", "--failure", json.dumps(finding_b1()),
                        "--code-quality", "harness/report.py: the totals loop is copied three times",
                        "--code-quality", "harness/report.py: one 900-line file holds the whole report")
        self.assertEqual(code, 0)
        request = self.state()["qa_requests"][first["id"]]
        self.assertEqual((request["status"], request["board_decision"]["blocking"], request["board_decision"]["notes"]), ("failed", ["B1"], []))
        self.assertEqual(code_quality.notes(self.state(), self.task_of(first)),
                         ["harness/report.py: the totals loop is copied three times", "harness/report.py: one 900-line file holds the whole report"])

    def test_notes_on_a_pass_keep_it_a_pass_and_a_missing_flag_stores_nothing(self):
        first = self.request()
        qa = self.claim(first, certified=True)
        self.assertEqual(self.cli(first, qa, "passed", "TASK DONE: YES — all good", "--code-quality", "a.py: a hard-coded port"), 0)
        self.assertEqual(self.state()["qa_requests"][first["id"]]["status"], "passed")
        self.assertEqual(code_quality.notes(self.state(), self.task_of(first)), ["a.py: a hard-coded port"])
        second = self.request(changes="again")
        qa2 = self.claim(second, certified=True)
        self.assertEqual(self.cli(second, qa2, "passed", "TASK DONE: YES — all good"), 0)
        self.assertEqual(code_quality.notes(self.state(), self.task_of(second)), ["a.py: a hard-coded port"], "no flag changes nothing")

    def test_notes_never_turn_a_round_two_note_only_fail_into_a_block(self):
        _recorded, second = self.second_round()
        verdict = "TASK DONE: YES — the report shows right totals\nBLOCKING: the footer is two pixels low"
        qa = self.claim(second, certified=True)
        self.assertEqual(self.cli(second, qa, "failed", verdict, "--failure", json.dumps(frozen.finding("R2-1", letter="new", summary="the footer is two pixels low on the report page")),
                                  "--code-quality", "harness/report.py: a missing seam to add another report"), 0)
        done = self.state()["qa_requests"][second["id"]]
        self.assertEqual((done["status"], done["board_decision"]["blocking"], [n["id"] for n in done["board_decision"]["notes"]]), ("passed", [], ["R2-1"]))
        self.assertEqual(code_quality.notes(self.state(), self.task_of(second)), ["harness/report.py: a missing seam to add another report"])
        self.assertEqual(self.owner_cards(), [])

    def test_lines_are_trimmed_cleaned_and_capped_never_refused(self):
        self.assertEqual(code_quality.clean(["  a.py:   x  ", "", None, "a.py: x"]), ["a.py: x"])
        self.assertEqual(code_quality.clean("one line"), ["one line"])
        self.assertEqual(code_quality.clean(None), [])
        long = code_quality.clean(["z" * 1000])[0]
        self.assertLessEqual(len(long), code_quality.MAX_CHARS)
        self.assertEqual(len(code_quality.clean([f"n{i}" for i in range(50)])), code_quality.MAX_NOTES)
        state: dict = {}
        self.assertFalse(code_quality.record(state, "T", "R", ["", "  "]))
        self.assertEqual(state, {})

    def test_the_end_of_task_history_carries_the_notes_and_the_page_draws_them_escaped(self):
        from pathlib import Path
        from harness import board_viewer
        source = Path(board_viewer.__file__).read_text(encoding="utf-8")
        self.assertIn('"code_quality": code_quality.notes(state, task)', source)
        self.assertIn("<li>${esc(note)}</li>", source[source.index("data-code-quality"):])


def finding_b1():
    return frozen.finding("B1")


class QualityRequirement(integrity._Project):
    def file_requirements(self, criteria):
        board.record_requirement_proposal(self.root, self.dev["id"], TEXT)
        self.inbox()
        board.record_contract_check_submission(self.root, self.qa["id"], TASK, 1, write_json(self.base, "sub.json", {"criteria": criteria}))
        return board.snapshot(self.root)["requirement_proposals"][TASK]["contract_check"]

    def test_the_kind_is_read_from_the_field_or_the_leading_words_and_never_refused(self):
        item = {"text": "x" * 8}
        self.assertEqual(contract_check.requirement_kind({"kind": "non-functional"}, "The page loads in under 2 seconds"), "non-functional")
        self.assertEqual(contract_check.requirement_kind({"kind": "Non Functional"}, "anything at all"), "non-functional")
        self.assertEqual(contract_check.requirement_kind(item, "Non-functional: works on a phone-width screen"), "non-functional")
        self.assertEqual(contract_check.requirement_kind(item, "Quality requirement: loads fast"), "non-functional")
        self.assertEqual(contract_check.requirement_kind(item, "Running the program prints hello"), "")
        self.assertEqual(contract_check.requirement_kind({"kind": "banana"}, "Running the program prints hello"), "")
        self.assertEqual(contract_check.requirement_kind({"kind": 7}, "Running the program prints hello"), "")

    def test_it_is_stored_labelled_and_covered_exactly_like_any_other_requirement(self):
        plain = "Protects the speed you asked for."
        check = {"coverage": "checked", "ledger": integrity.ledger("c1-1"), "files": {"test_needs.py": integrity.NEEDS_FILE}}
        other = {"coverage": "checked", "ledger": integrity.ledger("c2-1"), "files": {"test_needs.py": integrity.NEEDS_FILE}}
        block = self.file_requirements([
            {"text": "The greeting file is ready quickly", "kind": "non-functional", "plain": plain, "checks": [check], "failure_paths": ["a slow start is caught"]},
            {"text": "The greeting file is ready always", "plain": plain, "checks": [other], "failure_paths": ["a slow start is caught"]},
            {"text": "Non-functional: nothing here can be tested", "plain": plain, "why_not_checked": "a feeling"},
        ])
        first, second, third = block["criteria"]
        self.assertEqual((first["kind"], second["kind"], third["kind"]), ("non-functional", "", "non-functional"))
        self.assertEqual(first["coverage_line"], second["coverage_line"], "same coverage line as a functional requirement")
        self.assertTrue(third["coverage_line"].startswith("gap"), "a quality requirement without a test is a gap like any other")
        labels = contract_check.labels(block["criteria"])
        self.assertEqual([item["kind"] for item in labels], ["non-functional", "", "non-functional"])
        self.assertEqual(labels[0]["label"], labels[1]["label"])

    def test_it_is_seen_failing_before_the_work_like_any_other_requirement(self):
        check = {"coverage": "checked", "ledger": integrity.ledger("c1-1"), "files": {"test_needs.py": integrity.NEEDS_FILE}}
        board.record_requirement_proposal(self.root, self.dev["id"], TEXT)
        self.inbox()
        value = {"criteria": [{"text": "Non-functional: the greeting file is there when asked", "kind": "non-functional", "checks": [check]}]}
        board.record_contract_check_submission(self.root, self.qa["id"], TASK, 1, write_json(self.base, "sub.json", value))
        token = board.snapshot(self.root)["requirement_proposals"][TASK]["contract_check"]["token"]
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=token)
        board.record_requirement_confirmation(self.root, self.dev["id"], TEXT)
        confirmed = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]
        self.assertEqual(confirmed["compare"], {"1": red_proof.FAILS_BEFORE})
        self.assertEqual(confirmed["red_proof"]["status"], "settled")
        self.assertEqual(confirmed["labels"][0]["kind"], "non-functional")


if __name__ == "__main__":
    unittest.main()
