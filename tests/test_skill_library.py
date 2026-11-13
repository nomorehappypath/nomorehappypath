# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Tests for harness.skill_library: the library folder, its checks, and the
protection it must have from any agent (direct, parent-folder, or board-run
command), per SKILL_LIBRARY_SPEC.md sections 3 and 4.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness import skill_library


class LibraryContentTests(unittest.TestCase):
    """The shipped library: all 12 skills present, checked, and carrying their real text."""

    def test_all_twelve_skills_are_present_and_checked_clean(self):
        skills = skill_library.checked_skills()
        self.assertEqual([slug for slug, _, _ in skill_library.SKILLS], [skill.slug for skill in skills])
        for skill in skills:
            self.assertIsNone(skill.problem, f"{skill.slug}: {skill.problem}")
            self.assertTrue(skill.full_description, skill.slug)
            self.assertTrue(skill.plain_name, skill.slug)
            self.assertTrue(skill.owner_description, skill.slug)

    def test_usable_skills_matches_all_twelve_when_nothing_is_broken(self):
        self.assertEqual(len(skill_library.usable_skills()), 12)
        self.assertEqual(skill_library.broken_skills(), [])

    def test_find_returns_the_named_skill_with_its_real_file(self):
        found = skill_library.find("backend")
        self.assertIsNotNone(found)
        self.assertEqual(found.plain_name, "Server (back end)")
        self.assertTrue(found.path.is_file())

    def test_find_returns_none_for_an_unknown_slug(self):
        self.assertIsNone(skill_library.find("not-a-real-skill"))

    def test_requirements_skill_constant_is_in_the_library(self):
        self.assertIsNotNone(skill_library.find(skill_library.REQUIREMENTS_SKILL))

    def test_qa_testing_browser_steps_match_nmhps_own_route_not_a_generic_browser(self):
        text = skill_library.find("qa-testing").path.read_text(encoding="utf-8")
        self.assertIn("screen-check", text)
        self.assertIn("screen-script", text)
        self.assertNotIn("check a phone width and a desktop width", text)
        self.assertNotIn("check the console for errors", text)
        self.assertNotIn("keyboard-only walk of the main journey", text)

    def test_every_edit_is_listed_old_beside_new_in_the_report(self):
        report = (skill_library.library_root() / "CHANGES.md").read_text(encoding="utf-8")
        self.assertIn("Old:", report)
        self.assertIn("New:", report)
        self.assertIn("qa-testing", report)

    def test_the_other_eleven_skills_are_word_for_word_unedited(self):
        for slug, _, _ in skill_library.SKILLS:
            if slug == "qa-testing":
                continue
            text = skill_library.find(slug).path.read_text(encoding="utf-8")
            self.assertNotIn("screen-check", text)
            self.assertNotIn("screen-script", text)


class LibraryDamageDetectionTests(unittest.TestCase):
    """A broken skill is detected and left off, never silently dropped or crashing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.harness_root = Path(self.tmp)
        shutil.copytree(skill_library.library_root(), self.harness_root / skill_library.LIBRARY_DIRNAME)

    def test_a_missing_skill_file_is_reported_not_crashed(self):
        shutil.rmtree(self.harness_root / skill_library.LIBRARY_DIRNAME / "backend")
        skills = skill_library.checked_skills(self.harness_root)
        broken = [skill for skill in skills if skill.slug == "backend"][0]
        self.assertIsNotNone(broken.problem)
        self.assertNotIn("backend", [skill.slug for skill in skill_library.usable_skills(self.harness_root)])

    def test_a_skill_missing_its_description_line_is_reported(self):
        path = self.harness_root / skill_library.LIBRARY_DIRNAME / "security" / "SKILL.md"
        path.write_text("---\nname: security\n---\nno description here\n", encoding="utf-8")
        broken = skill_library.find("security", self.harness_root)
        self.assertIsNone(broken)
        problems = {skill.slug: skill.problem for skill in skill_library.broken_skills(self.harness_root)}
        self.assertIn("security", problems)

    def test_other_eleven_skills_stay_usable_when_one_is_broken(self):
        shutil.rmtree(self.harness_root / skill_library.LIBRARY_DIRNAME / "payments")
        self.assertEqual(len(skill_library.usable_skills(self.harness_root)), 11)

    def test_a_symlink_escaping_its_own_folder_is_reported_as_a_problem(self):
        folder = self.harness_root / skill_library.LIBRARY_DIRNAME / "frontend"
        outside = self.harness_root / "outside.txt"
        outside.write_text("not part of any skill", encoding="utf-8")
        try:
            (folder / "escape.txt").symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unsupported on this filesystem")
        problems = {skill.slug: skill.problem for skill in skill_library.broken_skills(self.harness_root)}
        self.assertIn("frontend", problems)

    def test_a_file_linking_into_a_different_skills_folder_is_reported_not_just_outside_the_library(self):
        # Reviewer finding (second repair, same cause): checking entries only
        # against the LIBRARY root let a file inside one skill's folder link
        # into a DIFFERENT skill's own folder undetected, since that target
        # is still inside the library. The boundary for a skill's own
        # contents must be that skill's own folder, not the library at large.
        frontend = self.harness_root / skill_library.LIBRARY_DIRNAME / "frontend"
        backend = self.harness_root / skill_library.LIBRARY_DIRNAME / "backend"
        try:
            (frontend / "borrowed.md").symlink_to(backend / "SKILL.md")
        except OSError:
            self.skipTest("symlinks unsupported on this filesystem")
        problems = {skill.slug: skill.problem for skill in skill_library.broken_skills(self.harness_root)}
        self.assertIn("frontend", problems)
        self.assertNotIn("backend", problems)

    def test_a_skill_folder_that_is_itself_a_symlink_outside_the_library_is_reported(self):
        # Reviewer finding: _escapes_own_folder only checked files INSIDE a
        # skill's folder against that folder's own resolved self, so a skill
        # folder that is itself a symlink to somewhere outside the library
        # root (not merely outside its own folder) passed unnoticed.
        real_outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, real_outside, ignore_errors=True)
        (real_outside / "SKILL.md").write_text("---\nname: security\ndescription: planted\n---\n", encoding="utf-8")
        folder = self.harness_root / skill_library.LIBRARY_DIRNAME / "security"
        shutil.rmtree(folder)
        try:
            folder.symlink_to(real_outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unsupported on this filesystem")
        problems = {skill.slug: skill.problem for skill in skill_library.broken_skills(self.harness_root)}
        self.assertIn("security", problems)

    def test_body_text_that_merely_looks_like_metadata_is_not_accepted_as_the_real_thing(self):
        # Reviewer finding: the name/description regexes searched the WHOLE
        # file, so a skill with no real frontmatter but a body sentence that
        # happens to start a line with "name:" or "description:" was wrongly
        # treated as checked-clean.
        path = self.harness_root / skill_library.LIBRARY_DIRNAME / "backend" / "SKILL.md"
        path.write_text(
            "No real frontmatter here.\n\nSomewhere in the body:\nname: backend\ndescription: this is prose, not metadata\n",
            encoding="utf-8",
        )
        problems = {skill.slug: skill.problem for skill in skill_library.broken_skills(self.harness_root)}
        self.assertIn("backend", problems)


class LibraryGrowthTests(unittest.TestCase):
    """Final review (B2): a guide folder added after this module was written
    must be usable with no code or proposal edit - usable_skills() finds it
    by scanning the folder, never by a fixed, hardcoded table."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.harness_root = Path(self.tmp)
        shutil.copytree(skill_library.library_root(), self.harness_root / skill_library.LIBRARY_DIRNAME)

    def test_a_new_guide_folder_is_usable_with_no_code_or_proposal_edit(self):
        folder = self.harness_root / skill_library.LIBRARY_DIRNAME / "new-guide"
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            "---\nname: new-guide\ndescription: Full technical guidance for the new guide.\n---\nBody.\n",
            encoding="utf-8",
        )
        found = skill_library.find("new-guide", self.harness_root)
        self.assertIsNotNone(found)
        self.assertEqual(len(skill_library.usable_skills(self.harness_root)), 13)
        # No plain_name:/owner_description: given - both are derived, not left empty.
        self.assertEqual(found.plain_name, "New Guide")
        self.assertTrue(found.owner_description)

    def test_a_new_guide_folder_may_give_its_own_plain_name_and_owner_description(self):
        folder = self.harness_root / skill_library.LIBRARY_DIRNAME / "new-guide"
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            "---\nname: new-guide\ndescription: Full technical guidance.\n"
            "plain_name: Brand New Guide\nowner_description: What it does, in one line.\n---\nBody.\n",
            encoding="utf-8",
        )
        found = skill_library.find("new-guide", self.harness_root)
        self.assertEqual(found.plain_name, "Brand New Guide")
        self.assertEqual(found.owner_description, "What it does, in one line.")

    def test_the_original_twelve_keep_their_fixed_display_order_and_names(self):
        skills = skill_library.checked_skills(self.harness_root)
        self.assertEqual([slug for slug, _, _ in skill_library.SKILLS], [skill.slug for skill in skills])
        for skill in skills:
            self.assertIsNone(skill.problem)


if __name__ == "__main__":
    unittest.main()
