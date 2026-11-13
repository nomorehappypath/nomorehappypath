# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Test for the Delivery role card's skill-library instruction: when NMHP
gives the builder the library catalogue while it prepares a proposal, the
card tells it to propose only what the task needs, with a reason each, pass
them as --skills, and read requirements-architecture first for a full task.
The board-side plumbing (board.skill_library_catalogue, the --skills flag
itself) is proven in the proposal-flow-boardpy-tests subtask instead, since
this subtask owns only directives/cards/DELIVERY.md.
"""
from __future__ import annotations

import unittest
from pathlib import Path


class DeliveryCardSkillLibraryRuleTests(unittest.TestCase):
    def setUp(self):
        self.text = " ".join(Path("directives/AGENT.md").read_text(encoding="utf-8").split()) + Path("directives/cards/DELIVERY.md").read_text(encoding="utf-8")   # the rule is in the requirements chapter; the card points to --skills (card size limit)

    def test_the_card_tells_the_builder_to_propose_only_what_is_needed(self):
        self.assertIn("propose only the ones this task actually needs", self.text)
        self.assertIn('never every one "to be safe"', self.text)

    def test_the_card_names_the_skills_flag(self):
        self.assertIn("--skills", self.text)

    def test_the_card_tells_the_builder_to_read_requirements_architecture_for_a_full_task(self):
        self.assertIn("requirements-architecture", self.text)
        self.assertIn("full task", self.text)


if __name__ == "__main__":
    unittest.main()
