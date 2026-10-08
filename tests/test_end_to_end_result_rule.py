# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Batch 2 item H: the reviewer directive carries the owner's end-to-end result rule.

On 2026-09-30 a reviewer passed Studio's Logo upgrade on fixtures while the
owner's real run showed no logo. The owner's rule (verbatim below) must sit in
the Independent Reviewer workflow right after "What justifies a FAIL".
"""
from __future__ import annotations

import unittest
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1] / "directives" / "AGENT.md"

RULE = """\
**End-to-end result rule (owner, 2026-09-30).** The reviewer's job is to test
the product end to end and check the result the owner actually receives, not
only the code or the pieces. When a Studio feature produces something the owner
sees or uses (a logo, image, deck, document, film, file, or screen), never pass
it on unit checks, synthetic fixtures, stubs, or code reading alone.

Before any PASS that affects such an outcome, and always at final acceptance:

1. Inspect a REAL run's output exactly as the owner receives it:
   - the files in the owner's Deliverables folder;
   - the task page payload (result, artifacts and download links);
   - whether the promised artifact actually exists, opens, and shows what was
     asked for. Open the image, deck, or file and look at it.
2. Trace the owner's path. Start the service the way the owner would, follow
   each choice or click, and confirm the finished task shows the result on the
   page and in Deliverables.
3. If no real run exists, FAIL for missing owner-visible proof, or ask Delivery
   for a real run before deciding. The same applies when the proof relies on
   placeholders, stubbed verification, or fixtures built to match the code's
   assumptions.

Code-level and fixture checks may supplement this check but never replace it.

The material-only rule limits what blocks; it never limits how thoroughly the
owner-visible result is checked. A missing or unusable promised result is always
material."""


class EndToEndResultRuleTests(unittest.TestCase):
    def test_the_owner_rule_is_verbatim_right_after_what_justifies_a_fail(self):
        text = AGENT.read_text(encoding="utf-8")
        self.assertEqual(text.count(RULE), 1, "the owner's rule must appear verbatim, once")
        workflow = text.index("## Independent Reviewer workflow")
        fail_rule = text.index("**What justifies a FAIL (owner, 2026-09-29).**")
        rule = text.index(RULE)
        ledger = text.index("The Challenge Ledger admits material rows only")
        self.assertLess(workflow, fail_rule)
        self.assertLess(fail_rule, rule)
        self.assertLess(rule, ledger, "inserted before the Challenge Ledger paragraph")
        between = text[fail_rule:rule]
        self.assertTrue(between.rstrip().endswith("material-rows-only rule for the Challenge Ledger below."),
                        "immediately after the What-justifies-a-FAIL paragraph")




class NotRunIsNotPassedRuleTests(unittest.TestCase):
    """2026-10-07: the reviewer said TASK DONE: YES for a step it could not run with a real AI."""

    def test_product_directive_says_not_run_is_not_passed_and_never_asks_the_owner_for_a_token(self):
        from pathlib import Path
        text = " ".join((Path(__file__).resolve().parents[1] / "directives" / "AGENT.md").read_text().split())
        for words in ("Not run is not passed", "TASK DONE: NOT TESTED", "is not a PASS",
                      "The board refuses to offer the project to the owner without `TASK DONE: YES`",
                      "never ask the owner to create, copy or paste a token or login"):
            self.assertIn(words, text)


class ReviewerRealClaudeHowToTests(unittest.TestCase):
    """2026-10-07: the Reviewer could not know how to use the token the app now gives it."""

    def test_reviewer_directive_says_how_to_run_a_real_claude_and_never_to_leak_or_request_the_token(self):
        from pathlib import Path
        text = " ".join((Path(__file__).resolve().parents[1] / "directives" / "AGENT.md").read_text().split())
        for words in ("HARNESS_REVIEWER_CLAUDE_TOKEN",
                      'CLAUDE_CODE_OAUTH_TOKEN="$HARNESS_REVIEWER_CLAUDE_TOKEN" claude',
                      "start it normally from your shell",
                      "Never print, echo, log, or write the token",
                      "never ask the owner to create or paste one",
                      "run in a clean environment without it",
                      "ask only for your terminal to be relaunched from Mission Control"):
            self.assertIn(words, text)


if __name__ == "__main__":
    unittest.main()
