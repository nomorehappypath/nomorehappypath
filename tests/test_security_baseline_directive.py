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

from harness import reference_chapters

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "directives" / "AGENT.md"
CTO = ROOT / "directives" / "CTO.md"
HEADING = "## security-baseline"      # directive diet (2026-10-25): the baseline is the chapter of that name
REVIEWER_BLOCK = "**Security baseline (owner, 2026-10-02).**"
WORD_CAP = 360


def flat(text: str) -> str:
    return " ".join(text.split())


def baseline_section(text: str) -> str:
    """The Delivery half of the chapter: everything before the Reviewer's checking rule that sits in the same chapter."""
    chapter = reference_chapters.chapters(text)["security-baseline"]
    return chapter[:chapter.index(REVIEWER_BLOCK)]


class SecurityBaselineTests(unittest.TestCase):
    def setUp(self):
        self.text = AGENT.read_text(encoding="utf-8")

    def test_the_baseline_is_one_chapter_that_both_cards_point_at(self):
        self.assertEqual(self.text.count(HEADING + "\n"), 1)
        for role in ("engineering", "qa"):
            self.assertTrue(any("security-baseline" in targets for _n, _s, targets in reference_chapters.card_rules(role)), role)

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
            "Every error goes through the Central error sink (chapter `error-sink`)",  # 2026-10-12
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
        quality_bar = flat(reference_chapters.chapters(self.text)["materiality"])
        material = quality_bar[quality_bar.index("Material, and always in scope"):quality_bar.index("Procedural, and never admitted")]
        self.assertIn("an applicable item of the Minimum security baseline (chapter `security-baseline`) missing or broken", material)

    def test_the_reviewer_checks_it_only_where_it_applies(self):
        reviewer = flat(reference_chapters.chapters(self.text)["security-baseline"])
        self.assertIn("**Security baseline (owner, 2026-10-02).**", reviewer)
        self.assertIn("only where the app has that feature", reviewer)
        self.assertIn("A missing or broken applicable item is material: FAIL on it.", reviewer)
        self.assertIn("Never demand an item for a feature the app does not have", reviewer)
        # Owner 2026-10-12: enterprise controls are now the per-task Enhanced
        # security checkbox; the reviewer never demands them while it is off.
        self.assertNotIn("never fail an app for lacking enterprise-grade controls", reviewer)
        self.assertIn("never fail an app for lacking Enhanced security deliverables while the task's checkbox is off", reviewer)

    def test_the_cto_release_gate_points_at_it(self):
        cto = flat(CTO.read_text(encoding="utf-8"))
        self.assertIn("a missing applicable item of the Minimum security baseline in `directives/AGENT.md` is one", cto)


if __name__ == "__main__":
    unittest.main()
