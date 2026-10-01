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


if __name__ == "__main__":
    unittest.main()
