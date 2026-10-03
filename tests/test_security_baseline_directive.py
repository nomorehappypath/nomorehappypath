# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner 2026-10-02: every app an agent builds gets a minimum security baseline.

"when a professional build an app, he/she makes sure there is a minimum
security. not exaggerated ones. we are not building top secret apps."

Before this, directives/AGENT.md named security once, as a ledger category,
and the quality bar's "would change what the owner receives" let it be
dismissed. The baseline must be present, applied only where the app has the
feature, material when it applies, checked by the reviewer only there, and
short enough that it never grows into an exaggerated section.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "directives" / "AGENT.md"
CTO = ROOT / "directives" / "CTO.md"
HEADING = "### Minimum security baseline"
WORD_CAP = 360


def flat(text: str) -> str:
    return " ".join(text.split())


def baseline_section(text: str) -> str:
    start = text.index(HEADING)
    end = text.index("\n### ", start + len(HEADING))
    return text[start:end]


class SecurityBaselineTests(unittest.TestCase):
    def setUp(self):
        self.text = AGENT.read_text(encoding="utf-8")

    def test_the_baseline_sits_once_in_the_delivery_workflow(self):
        self.assertEqual(self.text.count(HEADING), 1)
        position = self.text.index(HEADING)
        self.assertLess(self.text.index("## Delivery Agent workflow"), position)
        self.assertLess(position, self.text.index("## Independent Reviewer workflow"))

    def test_every_professional_default_is_covered(self):
        section = flat(baseline_section(self.text))
        for needle in (
            "No API keys, passwords or tokens in code",  # secrets
            "keep `.env` files out of git",
            "Validate and bound every input on the server",  # input
            "parameterised queries",  # injection
            "output escaping",
            "Hash passwords with a standard slow algorithm",  # logins
            "repeated failed attempts",  # brute force
            "`HttpOnly`",
            "CSRF protection",
            "may act on the record they ask for",  # access to records
            "Rate-limit any public endpoint that spends money",  # costly endpoints
            "never in a response",  # leaked errors
            "standard audit once before delivery",  # dependencies
            "Limit uploads by type and size",  # uploads
            "Debug mode",  # delivered settings
        ):
            self.assertIn(needle, section)

    def test_it_is_proportionate_and_applies_only_where_the_app_has_the_feature(self):
        section = baseline_section(self.text)
        self.assertIn("**Apply an item only if the app has that feature.**", flat(section))
        self.assertIn("not exaggerated", section)
        self.assertIn("Not a row per item", section)
        words = len(re.findall(r"\S+", section))
        self.assertLessEqual(words, WORD_CAP, "the baseline must stay short, not grow into a section")

    def test_the_quality_bar_cannot_dismiss_an_applicable_item(self):
        quality_bar = flat(self.text[self.text.index("### Delivery quality bar"):self.text.index(HEADING)])
        material = quality_bar[quality_bar.index("Material, and always in scope"):quality_bar.index("Procedural, and never admitted")]
        self.assertIn("an applicable item of the Minimum security baseline below missing or broken", material)

    def test_the_reviewer_checks_it_only_where_it_applies(self):
        reviewer = flat(self.text[self.text.index("## Independent Reviewer workflow"):])
        self.assertIn("**Security baseline (owner, 2026-10-02).**", reviewer)
        self.assertIn("only where the app has that feature", reviewer)
        self.assertIn("A missing or broken applicable item is material: FAIL on it.", reviewer)
        self.assertIn("Never demand an item for a feature the app does not have", reviewer)
        self.assertIn("never fail an app for lacking enterprise-grade controls", reviewer)

    def test_the_cto_release_gate_points_at_it(self):
        cto = flat(CTO.read_text(encoding="utf-8"))
        self.assertIn("a missing applicable item of the Minimum security baseline in `directives/AGENT.md` is one", cto)


if __name__ == "__main__":
    unittest.main()
