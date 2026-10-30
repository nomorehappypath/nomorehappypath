# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A scenario ledger with ids like S1 was refused with "no concrete scenario rows" and no reason (2026-10-28: ten refusals in a row).

Run:  PYTHONPATH=. python3 -m unittest tests.test_ledger_refusal_hint -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import contract

HEADER = "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n|---|---|---|---|---|---|\n"


def rows_for(first_cell, command="python3 -m unittest tests.test_a"):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.md"
        path.write_text(HEADER + f"| {first_cell} | The report lists every finding with its place | {command} | all findings listed | all findings listed | PASS |\n", encoding="utf-8")
        return contract._scenario_rows(path)


class LedgerHintTests(unittest.TestCase):
    def test_an_id_the_board_cannot_read_is_named_with_the_rule(self):
        rows, problems = rows_for("Scenario 1")
        self.assertEqual(rows, [])
        joined = " ".join(problems)
        self.assertIn("'Scenario 1'", joined)
        self.assertIn("S-001", joined)
        self.assertIn("test runner", joined)

    def test_s1_style_ids_are_accepted_too(self):
        rows, _ = rows_for("S1")
        self.assertEqual([row["id"] for row in rows], ["S1"])

    def test_a_proper_id_still_parses_with_no_hint(self):
        rows, problems = rows_for("S-001")
        self.assertEqual([row["id"] for row in rows], ["S-001"])
        self.assertFalse([item for item in problems if "scenario ids" in item])

    def test_an_empty_ledger_keeps_the_old_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.md"
            path.write_text(HEADER, encoding="utf-8")
            _, problems = contract._scenario_rows(path)
        self.assertIn("ledger has no concrete scenario rows", problems)


if __name__ == "__main__":
    unittest.main()


class DescriptionTests(unittest.TestCase):
    def test_an_ampersand_in_plain_words_is_not_a_command(self):
        rows, problems = rows_for("S-001", command="python3 -m unittest tests.test_a")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.md"
            path.write_text(HEADER + "| S-001 | Finding 1.3: the Q&A lifetime cap can be passed by two requests at once | python3 -m unittest tests.test_a | ok | ok | PASS |\n", encoding="utf-8")
            rows, problems = contract._scenario_rows(path, require_owner_description=True)
        self.assertEqual([row["id"] for row in rows], ["S-001"])
        self.assertFalse([item for item in problems if "describe behavior" in item], problems)

    def test_a_description_that_is_really_a_command_is_still_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.md"
            path.write_text(HEADER + "| S-001 | python3 -m pytest tests/test_a.py -q | python3 -m unittest tests.test_a | ok | ok | PASS |\n", encoding="utf-8")
            _, problems = contract._scenario_rows(path, require_owner_description=True)
        self.assertTrue([item for item in problems if "not a command" in item], problems)


class CombinedRefusalTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"HARNESS_STANDING_CHECKS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_hand_in_with_two_problems_gets_one_message_with_both_and_the_format(self):
        from harness import board
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.md"
            ledger.write_text(HEADER + "| Scenario 1 | The report lists every finding | python3 -m unittest tests.test_a | ok | ok | PASS |\n", encoding="utf-8")
            message = board._hand_in_refusal(root, "Report delivered.", str(ledger))
        self.assertIn("2 things to fix", message)
        self.assertIn("Security:", message)
        self.assertIn("scenario ids", message)
        self.assertIn("| S-001 |", message)

    def test_one_problem_keeps_the_single_message(self):
        from harness import board
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.md"
            ledger.write_text(HEADER + "| S-001 | The report lists every finding with its place | python3 -m unittest tests.test_a | ok | ok | PASS |\n", encoding="utf-8")
            message = board._hand_in_refusal(root, "Report delivered.", str(ledger))
        self.assertNotIn("things to fix", message)
        self.assertIn("Security", message)

    def test_a_ledger_pasted_as_text_says_it_needs_a_path(self):
        from harness import board_client
        with self.assertRaisesRegex(board_client.ArtifactError, "PATH of a file"):
            board_client._read_artifact_checked("| ID | What |\n|---|---|\n| S-001 | x |", "ledger")
