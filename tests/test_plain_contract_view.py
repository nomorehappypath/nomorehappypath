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
from tests import examples_first
from tests.test_contract_first_checks import TEST_FILE, TASK, ledger, plain_ready, write_json
from tests.requirements_support import NO_SETUP, setup_checked
from tests.test_requirements_contract import context, delivery_with_direction

GOOD_BLOCK = "In plain words:\nA forecast page will show the next five days.\nYou will see a high and a low for each day.\nNothing for you to do."
FIT = "Already in the project:\nNothing related exists yet."
FULL = "Objective: five-day forecast.\nAcceptance: each day shows a high and a low."


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
        return examples_first.record(self.root, self.reviewer["id"], TASK, 1, str(path))

    def proposal(self):
        return board.snapshot(self.root)["requirement_proposals"][TASK]

    def test_the_confirmed_contract_keeps_the_sentence_for_the_owners_list(self):
        self.submit(self.value("The program tells you hello when you run it."))
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])
        agent = next(a for a in board.snapshot(self.root)["agents"].values() if a["role"] == "development")
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        labels = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]["labels"]
        self.assertEqual(labels[0]["plain"], "The program tells you hello when you run it.")
        self.assertEqual(board.snapshot(self.root)["requirement_confirmations"][TASK]["text"], GOOD_BLOCK + "\n\n" + FULL)


# A real Builder's proposal names its app's real run, with or without AI (task 2026-11-24_tdd-examples-first, Change 5).
WITH_REAL_RUN = json.dumps({"ai_tools": [], "real_run": "python3 run_it.py", "real_scenario": "A city goes in and its forecast comes out",
                            "real_output": "out/result.txt"})

class ProposalCommandTests(unittest.TestCase):
    """The agent's command (the board's own entry point) requires the plain block; the board's own callers and older fixtures do not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))
        self.agent = delivery_with_direction(self.root, TASK)
        setup_checked(self.root, TASK, WITH_REAL_RUN)

    def propose(self, text):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = board.main(["--root", str(self.root), "propose-requirements", "--agent", self.agent["id"], "--text", text, "--needs", WITH_REAL_RUN,
                               "--no-screens", "a fixture task: nothing visible changes"])
        return code, out.getvalue() + err.getvalue()

    def test_a_good_block_is_accepted_and_confirmed_word_for_word(self):
        code, output = self.propose(GOOD_BLOCK + "\n\n" + FIT + "\n\n" + FULL)
        self.assertEqual(code, 0, output)
        board.record_requirements_decision(self.root, TASK, "go_ahead")
        confirmation = board.record_requirement_confirmation(self.root, self.agent["id"], GOOD_BLOCK + "\n\n" + FIT + "\n\n" + FULL)
        self.assertEqual(board.snapshot(self.root)["requirement_confirmations"][TASK]["text"], GOOD_BLOCK + "\n\n" + FIT + "\n\n" + FULL)
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
