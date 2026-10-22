# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""QA evidence accepts every command the certified-command validator accepts (windows-walk item 9).

Run:  PYTHONPATH=. python3 -m unittest tests.test_evidence_command_line -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, contract
from tests.test_execute_once import ExecuteOnceTests

COMMANDS = [
    "TEST_MACHINE_GEN=2 make test", "make test", "cargo test", "cargo test --release", "go test ./...", "gradle test",
    "npm run test", "npm test", "pytest -q", "python3 -m unittest discover -s tests",
    # accepted before this change and still accepted
    "npm run integration", "X=$HOME pytest", "curl -s http://127.0.0.1:1/", "git status", "test -f a",
]


class EvidenceShapeTests(unittest.TestCase):
    def check(self, text: str):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "e.txt").write_text(text)
            return board._require_evidence_file(root, str(root / "e.txt"), "QA evidence")

    def test_every_certified_command_is_accepted_bare_prefixed_and_indented(self):
        for command in COMMANDS:
            for shape in ("{c}", "command: {c}", "$ {c}", "  Command: $ {c}", "COMMAND: {c}"):
                with self.subTest(command=command, shape=shape):
                    self.assertTrue(self.check(shape.format(c=command) + "\nresult: PASS\n"))

    def test_the_two_lists_cannot_drift(self):
        for command in ("make test", "cargo test", "go test ./...", "gradle test", "npm run test", "FOO=1 make test"):
            self.assertTrue(contract.EXECUTABLE_TEST_COMMAND.match(command))
            self.assertTrue(self.check("command: " + command + "\nresult: PASS\n"), command)

    def test_runner_output_is_a_result(self):
        for line in ("Ran 3 tests in 0.1s", "===== 3 passed in 0.2s =====", "test result: ok. 3 passed; 0 failed", "result: FAIL",
                     "SomeTest > example PASSED", "exit code: 1", "exit code: 0", "OK", "FAILED (failures=1)"):
            with self.subTest(line=line):
                self.assertTrue(self.check("command: make test\n" + line + "\n"))

    def test_refusals_name_the_missing_half_and_the_length(self):
        with self.assertRaisesRegex(ValueError, "no an executed command line.*Accepted example"):
            self.check("some notes about the run\nresult: PASS\n")
        with self.assertRaisesRegex(ValueError, "no a result line"):
            self.check("command: make test\nthe run went fine\n")
        with self.assertRaisesRegex(ValueError, "too short"):
            self.check("make test\nOK")
        with self.assertRaisesRegex(ValueError, "is missing"):
            board._require_evidence_file(Path("/nonexistent"), "nope.txt", "QA evidence")

    def test_a_token_is_still_refused(self):
        with self.assertRaisesRegex(ValueError, "Claude login token"):
            self.check("command: make test\nresult: PASS\nsk-ant-oat01-" + "a" * 40 + "\n")


class MakeTestThroughTheBoardTests(ExecuteOnceTests):
    """The owner's case: a board-certified `make test` ledger, then qa-result with evidence that records `make test`."""

    challenge_command = "make test"

    def certified(self):
        (self.root / "Makefile").write_text("test:\n\tpython3 -m unittest test_smoke\n")
        qa, request, _, evidence = self._claimed_review()
        board.execute_challenge(self.root, qa["id"], request["id"])
        again = board.execute_challenge(self.root, qa["id"], request["id"])
        self.assertEqual(again["status"], "already_certified")
        return qa, request, evidence, again

    def submit(self, qa, request, path):
        with patch.object(board, "_execute_scenario_simulations", side_effect=AssertionError("no rerun")) as run:
            recorded = board.qa_result(self.root, qa["id"], request["id"], "passed", "certified make test", str(path))
        run.assert_not_called()
        self.assertEqual(recorded["status"], "passed")

    def test_evidence_that_records_make_test_is_accepted_after_certification(self):
        qa, request, evidence, _ = self.certified()
        evidence.write_text("command: TEST_MACHINE_GEN=2 make test\nRan 1 test\nresult: PASS\n")
        self.submit(qa, request, evidence)

    def test_the_boards_own_certified_evidence_is_accepted(self):
        qa, request, _, again = self.certified()
        self.submit(qa, request, again["evidence"])

    # the inherited tests are covered by test_execute_once; do not run them twice
for _name in [n for n in dir(ExecuteOnceTests) if n.startswith("test_")]:
    setattr(MakeTestThroughTheBoardTests, _name, None)


if __name__ == "__main__":
    unittest.main()
