# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Tests for the board.py plumbing that lets a builder's proposal carry
skill-library picks: board.skill_library_catalogue() (what the builder is
given) and board._validate_proposed_skills() (splitting what it proposes
into accepted and dropped) — SKILL_LIBRARY_SPEC.md section 3, requirement 5
of build-skill-library.
"""
from __future__ import annotations

import unittest

from harness import board, skill_library


class SkillLibraryCatalogueTests(unittest.TestCase):
    """What the builder is given while preparing a proposal."""

    def test_catalogue_has_all_twelve_with_real_name_and_full_description(self):
        catalogue = board.skill_library_catalogue()
        self.assertEqual(len(catalogue), 12)
        by_slug = {item["slug"]: item for item in catalogue}
        backend = by_slug["backend"]
        self.assertEqual(backend["plain_name"], "Server (back end)")
        self.assertTrue(backend["owner_description"])
        # The FULL SKILL.md description, not the short owner-facing line (owner repair: not shortened).
        self.assertIn("API endpoints, business rules, background jobs", backend["full_description"])
        self.assertNotEqual(backend["full_description"], backend["owner_description"])

    def test_requirements_skill_constant_is_in_the_catalogue(self):
        slugs = {item["slug"] for item in board.skill_library_catalogue()}
        self.assertIn(skill_library.REQUIREMENTS_SKILL, slugs)


class ValidateProposedSkillsTests(unittest.TestCase):
    """board._validate_proposed_skills: accepted vs dropped."""

    def test_a_real_skill_with_a_reason_is_accepted(self):
        accepted, dropped = board._validate_proposed_skills(
            [{"name": "backend", "reason": "the booking rules need server code"}],
        )
        self.assertEqual(dropped, [])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["slug"], "backend")
        self.assertEqual(accepted[0]["plain_name"], "Server (back end)")
        self.assertEqual(accepted[0]["reason"], "the booking rules need server code")

    def test_a_name_not_in_the_library_is_dropped_not_refused(self):
        accepted, dropped = board._validate_proposed_skills(
            [{"name": "backend", "reason": "ok"}, {"name": "not-a-real-skill", "reason": "ok"}],
        )
        self.assertEqual([item["slug"] for item in accepted], ["backend"])
        self.assertEqual(dropped, ["not-a-real-skill"])

    def test_a_repeated_name_is_deduplicated_keeping_the_first(self):
        accepted, dropped = board._validate_proposed_skills(
            [{"name": "backend", "reason": "first"}, {"name": "backend", "reason": "second"}],
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["reason"], "first")

    def test_an_empty_or_malformed_list_proposes_nothing_and_drops_nothing(self):
        self.assertEqual(board._validate_proposed_skills([]), ([], []))
        self.assertEqual(board._validate_proposed_skills(None), ([], []))
        self.assertEqual(board._validate_proposed_skills("not a list"), ([], []))

    def test_malformed_entries_inside_the_list_are_skipped_not_fatal(self):
        accepted, dropped = board._validate_proposed_skills(["not-a-dict", {"name": "backend", "reason": "ok"}])
        self.assertEqual([item["slug"] for item in accepted], ["backend"])
        self.assertEqual(dropped, [])

    def test_a_reason_is_truncated_not_refused_when_too_long(self):
        accepted, _ = board._validate_proposed_skills([{"name": "backend", "reason": "x" * 500}])
        self.assertLessEqual(len(accepted[0]["reason"]), 160)


class BeginTaskCatalogueTests(unittest.TestCase):
    """A new builder's begin_task result itself carries the catalogue (reviewer finding B1)."""

    def test_begin_task_result_carries_the_full_catalogue(self):
        import tempfile
        from pathlib import Path

        from harness import control
        from tests.test_requirements_contract import context

        with tempfile.TemporaryDirectory() as td:
            root = context(Path(td))
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            board.record_owner_direction(root, session["id"], "OWNER DIRECTION — TASK-BEGIN-CATALOGUE: build a thing.")
            result = board.begin_task(root, agent["id"], "TASK-BEGIN-CATALOGUE")
            slugs = {item["slug"] for item in result.get("skill_library", [])}
            self.assertEqual(len(slugs), 12)


class RecordRequirementProposalSkillsTests(unittest.TestCase):
    """The skills argument reaches the stored proposal record."""

    def test_proposal_record_stores_skills_proposed_and_dropped(self):
        import tempfile
        from pathlib import Path

        from tests.test_requirements_contract import context, delivery_with_direction

        with tempfile.TemporaryDirectory() as td:
            root = context(Path(td))
            agent = delivery_with_direction(root, "TASK-SKILLS")
            event = board.record_requirement_proposal(
                root, agent["id"], "Final agreed requirements: build the thing.",
                skills=[{"name": "backend", "reason": "server code needed"}, {"name": "nope", "reason": "x"}],
            )
            proposal = board.snapshot(root)["requirement_proposals"][event["task"]]
            self.assertEqual([item["slug"] for item in proposal["skills_proposed"]], ["backend"])
            self.assertEqual(proposal["skills_dropped"], ["nope"])


if __name__ == "__main__":
    unittest.main()
