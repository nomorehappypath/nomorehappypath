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
    def test_the_reviewer_s_rules_text_says_so(self):
        text = flat(ROOT / "harness" / "review_brief.py").replace('" "', "")   # adjacent string literals read as one sentence
        self.assertIn("docstrings or wording", text)
        self.assertIn("never a declared check that can fail the round", text)
        self.assertIn("A round whose only failures are such notes is a PASS", text)

    def test_the_agents_directive_says_so(self):
        text = flat(ROOT / "harness" / "directives" / "00_SPAWN_DEVELOPMENT_DIRECTIVE.md")
        self.assertIn("never a declared check that can fail a round", text)
        self.assertIn("a round whose only failures are such notes is a PASS", text)

    def test_the_delivered_reviewer_card_and_the_served_reference_say_so(self):
        """Codex R1-1 (2026-11-05): the startup card (directives/cards/REVIEWER.md, served by scripts/run_managed_agent.sh) and
        the `review-workflow` reference (directives/AGENT.md) are what a managed Reviewer actually reads; the owner's 2026-11-03
        FAIL order there is what produced the two docstring rounds, so these two texts are pinned too."""
        card = flat(ROOT / "directives" / "cards" / "REVIEWER.md")
        self.assertIn("never a FAIL and never a declared check; a round whose only failures are such notes is a PASS", card)
        self.assertNotIn("why-comment is material: FAIL", card)
        reference = flat(ROOT / "directives" / "AGENT.md")
        self.assertIn("missing is recorded as a note naming each gap by file:line", reference)
        self.assertIn("a round whose only failures are such notes is a PASS", reference)
        self.assertNotIn("Any of them missing is material: FAIL", reference)
        self.assertIn("the task created or changed is a note, never material", reference)


if __name__ == "__main__":
    unittest.main()
