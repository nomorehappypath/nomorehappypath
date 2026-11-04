# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Delivery directive makes the code it writes document itself (owner 2026-11-03)."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CodeDocumentsItselfRuleTests(unittest.TestCase):
    def setUp(self):
        text = (ROOT / "directives" / "AGENT.md").read_text(encoding="utf-8")
        start = text.index("## delivery-workflow")
        self.chapter = text[start:text.index("\n## ", start + 5)]

    def test_files_functions_and_reasons_are_documented(self):
        self.assertIn("Code you write documents itself", self.chapter)
        self.assertIn("the file opens with a short note saying what it does", self.chapter)
        self.assertIn("says what it\ntakes and what it returns", self.chapter)
        self.assertIn("a comment explains why wherever the reason is not\nobvious", self.chapter)

    def test_it_limits_the_rule_to_touched_files_and_useful_comments(self):
        self.assertIn("Do not comment what the code plainly says", self.chapter)
        self.assertIn("do not go back\nand document files the task does not touch", self.chapter)


class ReviewerChecksTheDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "directives" / "AGENT.md").read_text(encoding="utf-8")

    def chapter(self, name):
        start = self.text.index(f"## {name}")
        return self.text[start:self.text.index("\n## ", start + 5)]

    def test_the_reviewer_fails_missing_documentation_by_file_and_line(self):
        review = self.chapter("review-workflow")
        self.assertIn("Check the in-code documentation", review)
        self.assertIn("Any of them missing is material: FAIL and\nname each gap by file:line", review)
        self.assertIn("Files the task did not touch, and the wording of a note that is there, are\nnever a reason to fail", review)

    def test_materiality_lists_missing_documentation_as_material(self):
        self.assertIn("the in-code\n  documentation of chapter `delivery-workflow` missing", self.chapter("materiality"))

    def test_both_role_cards_carry_the_rule(self):
        delivery = (ROOT / "directives" / "cards" / "DELIVERY.md").read_text(encoding="utf-8")
        reviewer = (ROOT / "directives" / "cards" / "REVIEWER.md").read_text(encoding="utf-8")
        self.assertIn("open with what the file does, say what each function you add or change takes and returns", delivery)
        self.assertIn("or why-comment is material: FAIL naming each gap by file:line", reviewer)


if __name__ == "__main__":
    unittest.main()
