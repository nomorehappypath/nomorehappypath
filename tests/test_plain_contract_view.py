# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The contract and its tests are in plain words for the owner (task 2026-10-24_plain-contract-view): the board's side.

Run:  PYTHONPATH=. python3 -m unittest tests.test_plain_contract_view -v
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from harness import board, contract_check, control
from tests.test_contract_first_checks import TEST_FILE, TASK, ledger, plain_ready, write_json
from tests.test_requirements_contract import context, delivery_with_direction

GOOD_BLOCK = "In plain words:\nA forecast page will show the next five days.\nYou will see a high and a low for each day.\nNothing for you to do."
FULL = "Objective: five-day forecast.\nAcceptance: each day shows a high and a low."


class LintTests(unittest.TestCase):
    def test_every_developer_pattern_is_refused_with_a_reason(self):
        bad = {
            "a backtick": "The `forecast` page shows five days to the owner",
            "a section number": "The spec's section 15 coverage table holds for owner use",
            "a file name": "The forecast_view.py page shows five days of weather",
            "GIVEN / WHEN / THEN": "GIVEN a city WHEN opened THEN five days show up",
            "the word spec or delta": "Delta three of the page shows five days for you",
            "a function call": "The page calls build_days() to show five days",
            "a code name": "The forecast_view page shows five days to the owner",
            "a decision number": "The page follows D-004 and shows five days",
            "a commit or code id": "Built in 4efdf2f9abe7 and shows five days for the owner",
        }
        for name, text in bad.items():
            with self.subTest(name):
                reason = contract_check.plain_problem(text)
                self.assertTrue(reason, text)
        self.assertIn("section number", contract_check.plain_problem(bad["a section number"]))
        self.assertIn("characters", contract_check.plain_problem("word " * 40))
        self.assertIn("too short", contract_check.plain_problem("Everything will work"))

    def test_ordinary_plain_english_passes(self):
        for text in ("Your work stays saved when you close the window", "Click Save, then continue with the form to go on",
                     "Every service except film gets the questions step", "Version 2024 shows 12345678 rows to the owner here",
                     "You are given a download when the report is done"):
            with self.subTest(text):
                self.assertEqual(contract_check.plain_problem(text), "")

    def test_the_limits_are_exact_and_non_text_is_refused(self):
        exactly = ("a " * 70).strip()
        self.assertEqual(len(exactly), 139)
        self.assertEqual(contract_check.plain_problem(exactly + "b"), "")                    # 140
        self.assertNotEqual(contract_check.plain_problem(exactly + "bc"), "")                 # 141
        for value in (None, 5, "", "   ", ["a"]):
            self.assertEqual(contract_check.plain_problem(value), "it is empty")


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = context(self.base)
        agent = delivery_with_direction(self.root, TASK)
        session = control.create(self.root, "claude_reviewer")
        self.reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        control.take_instructions(self.root, session["id"])
        board.record_requirement_proposal(self.root, agent["id"], GOOD_BLOCK + "\n\n" + FULL)

    def value(self, plain):
        check = {"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE}}
        return {"criteria": [{"text": "Running the program prints hello", "checks": [check], **({} if plain is None else {"plain": plain})}], "questions": []}

    def submit(self, value):
        path = self.base / "s.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return board.record_contract_check_submission(self.root, self.reviewer["id"], TASK, 1, str(path))

    def proposal(self):
        return board.snapshot(self.root)["requirement_proposals"][TASK]

    def test_a_missing_or_developer_style_sentence_refuses_the_whole_submission_and_stores_nothing(self):
        for plain in (None, "", "The spec's section 15 coverage table holds for this", "Everything works"):
            with self.subTest(plain=plain), self.assertRaisesRegex(ValueError, "criterion 1 needs a `plain` sentence"):
                self.submit(self.value(plain))
        check = self.proposal()["contract_check"]
        self.assertEqual((check["status"], "criteria" in check), ("reading", False), "a refusal leaves the look open and stores nothing")
        # the Reviewer fixes it before the deadline: accepted, and the owner's Go ahead was never held
        self.assertEqual(self.submit(self.value("The program tells you hello when you run it."))["status"], "ready")
        stored = self.proposal()["contract_check"]["criteria"][0]
        self.assertEqual(stored["plain"], "The program tells you hello when you run it.")
        self.assertEqual(contract_check.labels([stored])[0]["plain"], stored["plain"])

    def test_the_confirmed_contract_keeps_the_sentence_for_the_owners_list(self):
        self.submit(self.value("The program tells you hello when you run it."))
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])
        agent = next(a for a in board.snapshot(self.root)["agents"].values() if a["role"] == "development")
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        labels = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]["labels"]
        self.assertEqual(labels[0]["plain"], "The program tells you hello when you run it.")
        self.assertEqual(board.snapshot(self.root)["requirement_confirmations"][TASK]["text"], GOOD_BLOCK + "\n\n" + FULL)


class ProposalCommandTests(unittest.TestCase):
    """The agent's command (the board's own entry point) requires the plain block; the board's own callers and older fixtures do not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))
        self.agent = delivery_with_direction(self.root, TASK)

    def propose(self, text):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = board.main(["--root", str(self.root), "propose-requirements", "--agent", self.agent["id"], "--text", text])
        return code, out.getvalue() + err.getvalue()

    def test_missing_too_long_or_developer_blocks_are_refused_with_the_fix(self):
        cases = {
            "no block": FULL,
            "four lines": "In plain words:\nOne line of text here.\nTwo lines of text here.\nThree lines of text here.\nFour lines of text here.\n\n" + FULL,
            "no blank line and full text": "In plain words:\nA forecast page shows five days.",
            "line too long": "In plain words:\n" + ("word " * 41) + "\n\n" + FULL,
            "jargon": "In plain words:\nImplements delta 3 of spec section 15 in forecast_view.py\n\n" + FULL,
        }
        for name, text in cases.items():
            with self.subTest(name):
                code, output = self.propose(text)
                self.assertNotEqual(code, 0, output)
                self.assertIn("In plain words", output)
                self.assertIn("Good:", output)
        self.assertNotIn(TASK, board.snapshot(self.root).get("requirement_proposals", {}), "nothing was filed")

    def test_a_good_block_is_accepted_and_confirmed_word_for_word(self):
        code, output = self.propose(GOOD_BLOCK + "\n\n" + FULL)
        self.assertEqual(code, 0, output)
        board.record_requirements_decision(self.root, TASK, "go_ahead")
        confirmation = board.record_requirement_confirmation(self.root, self.agent["id"], GOOD_BLOCK + "\n\n" + FULL)
        self.assertEqual(board.snapshot(self.root)["requirement_confirmations"][TASK]["text"], GOOD_BLOCK + "\n\n" + FULL)
        self.assertTrue(confirmation)

    def test_the_boards_own_callers_are_not_refused(self):
        board.record_requirement_proposal(self.root, self.agent["id"], FULL)        # older fixtures and stored proposals
        self.assertEqual(board.snapshot(self.root)["requirement_proposals"][TASK]["text"], FULL)


class DirectiveTests(unittest.TestCase):
    def test_the_directives_and_the_kickoff_say_it(self):
        root = Path(__file__).resolve().parents[1]
        agent = (root / "directives" / "AGENT.md").read_text(encoding="utf-8")
        kickoff = (root / "scripts" / "run_managed_agent.sh").read_text(encoding="utf-8")
        self.assertIn('**The text starts with an "In plain words:" block for the owner**', agent)
        self.assertIn("Good: `In plain words:`", agent)
        self.assertIn("Bad: `Implements delta 3 of spec section 15", agent)
        self.assertIn('"plain": "<one sentence the OWNER can read', agent)
        self.assertIn("Good: \"Every service except film gets the questions step.\"", agent)
        self.assertIn("'In plain words:' block", kickoff)
        self.assertNotIn("After the owner\nagrees and says go ahead, write a structured final requirements confirmation", kickoff)


if __name__ == "__main__":
    unittest.main()
