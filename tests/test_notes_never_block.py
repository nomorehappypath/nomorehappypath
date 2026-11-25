# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Notes in the code never fail a review round (owner 2026-11-05, stranger run).

The weather app's final review took four rounds: round 1 found one real defect, rounds 2 and 3 failed on declared checks that
only asked for docstrings and opening notes ("the changed function explains what it takes and returns", "each dependency file
begins with an explanation"), and the round cap then started a fresh Delivery attempt for docstrings — about $11 on an app that
already worked. The rule the Reviewer and Delivery read now says such a check is never blocking and a round whose only failures
are such notes is a PASS. These tests pin the two texts that carry it.

Run:  PYTHONPATH=. python3 -m unittest tests.test_notes_never_block -v
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def flat(path: Path) -> str:
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


class NotesNeverBlockARound(unittest.TestCase):
    """2026-11-15 the owner made missing code documentation a bug again ("if a directive says document the code, if code is not
    documented this is a bug"); every other theoretical or wording note still never costs a round. These tests pin both halves."""

    def test_the_reviewer_s_rules_text_says_so(self):
        text = flat(ROOT / "harness" / "review_brief.py").replace('" "', "")   # adjacent string literals read as one sentence
        self.assertIn("a theoretical variant of a case already handled", text)
        self.assertIn("is a note, never a blocker or another round", text)
        self.assertIn("Code documentation is the exception (owner 2026-11-15)", text)
        self.assertIn("is a bug: FAIL, naming each gap by file:line", text)
        self.assertNotIn("A round whose only failures are such notes is a PASS", text)

    def test_the_delivered_reviewer_card_and_the_served_reference_say_so(self):
        card = flat(ROOT / "directives" / "cards" / "REVIEWER.md")
        self.assertIn("or why-comment is a bug (owner 2026-11-15): FAIL naming each gap by file:line", card)
        reference = flat(ROOT / "directives" / "AGENT.md")
        self.assertIn("the task created or changed is a bug, so material (owner 2026-11-15)", reference)
        self.assertNotIn("missing is recorded as a note naming each gap by file:line", reference)

if __name__ == "__main__":
    unittest.main()
