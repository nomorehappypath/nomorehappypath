# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""R10: "This app gets Support" is a project-level choice (New project tick, or the line in Give
direction for an existing project), snapshotted onto each task when it begins so a later change
never retargets a task already building — the same discipline task_kinds already follows.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, project_manager_page
from tests.test_requirements_contract import context, delivery_with_direction


class SupportPlanChoiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))

    def test_a_new_project_is_off_until_the_owner_ticks_it(self):
        agent = delivery_with_direction(self.root, "TASK-SUPPORT-PLAN")
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertEqual(bundle["support_plan"]["on"], False)

    def test_ticking_it_turns_on_for_the_next_task_begun(self):
        result = board.set_project_support_plan(self.root, True)
        self.assertTrue(result["on"])
        agent = delivery_with_direction(self.root, "TASK-SUPPORT-PLAN-ON")
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertEqual(bundle["support_plan"]["on"], True)

    def test_a_later_change_never_retargets_a_task_already_begun(self):
        agent = delivery_with_direction(self.root, "TASK-ALREADY-RUNNING")
        board.set_project_support_plan(self.root, True)
        bundle = board.poll(self.root, agent["id"])["context_bundle"]["tasks"][0]
        self.assertEqual(bundle["support_plan"]["on"], False, "a running task keeps the choice it began with")

    def test_new_project_and_help_offer_the_tick_in_the_owners_own_words(self):
        page = project_manager_page.PAGE
        self.assertIn("This app gets Support", page)
        self.assertIn("SUPPORT_PLAN.md", page)
        self.assertIn("nmhp.support.check", page)


if __name__ == "__main__":
    unittest.main()
