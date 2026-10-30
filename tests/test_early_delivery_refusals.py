# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The refusals a fresh Delivery agent hit first (2026-10-28): format before task, a too-strict heading rule, no begin-task in the kickoff.

Run:  PYTHONPATH=. python3 -m unittest tests.test_early_delivery_refusals -v
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from harness import board, contract_check

BODY = "FULL REQUIREMENTS\n\nObjective: write the report."


class HeadingTests(unittest.TestCase):
    def test_the_label_on_its_own_line_still_works(self):
        block, text = contract_check.split_plain_summary("In plain words:\nI will write one report about the code.\nNothing for you to do.\n\n" + BODY)
        self.assertEqual(len(block), 2)
        self.assertTrue(text.startswith("In plain words:\n"))

    def test_the_label_with_the_first_line_after_it_is_accepted(self):
        block, text = contract_check.split_plain_summary(
            "In plain words: I will write one report about the code.\nNothing for you to do here.\n\n" + BODY)
        self.assertEqual(block, ["I will write one report about the code.", "Nothing for you to do here."])
        self.assertTrue(text.startswith("In plain words:\nI will write"))

    def test_no_block_is_still_refused(self):
        with self.assertRaises(ValueError):
            contract_check.split_plain_summary("Objective: write the report.")


class OrderTests(unittest.TestCase):
    def test_a_missing_task_is_reported_before_the_format(self):
        state = {"agents": {"A1": {"id": "A1", "role": "engineering", "task": board.AWAITING_OWNER_DIRECTION, "active": True}}}
        with mock.patch.object(board, "snapshot", return_value=state):
            message = board._proposal_not_ready(Path("/x"), "A1")
        self.assertIn("begin-task", message)

    def test_an_agent_with_a_task_is_not_blamed(self):
        state = {"agents": {"A1": {"id": "A1", "role": "engineering", "task": "t", "active": True}}}
        with mock.patch.object(board, "snapshot", return_value=state):
            self.assertEqual(board._proposal_not_ready(Path("/x"), "A1"), "")

    def test_a_badly_formatted_proposal_from_an_agent_without_a_task_names_the_task_first(self):
        state = {"agents": {"A1": {"id": "A1", "role": "engineering", "task": board.AWAITING_OWNER_DIRECTION, "active": True}}}
        with mock.patch.object(board, "snapshot", return_value=state):
            with self.assertRaises(ValueError) as caught:
                board.record_requirement_proposal(Path("/x"), "A1", "no heading at all", plain_required=True)
        self.assertIn("begin-task", str(caught.exception))


class KickoffTests(unittest.TestCase):
    def test_the_kickoff_tells_the_builder_to_begin_the_task(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "run_managed_agent.sh").read_text(encoding="utf-8")
        self.assertIn("run begin-task --agent ${agent_id} --task <short-plain-name>", script)


if __name__ == "__main__":
    unittest.main()
