# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The app itself tells a Code analysis what to look for and how to write FINDINGS.md (owner 2026-10-29), so the owner types only the project facts.

Run:  PYTHONPATH=. python3 -m unittest tests.test_analysis_report_contents -v
"""
from __future__ import annotations

import unittest

from harness import reference_chapters

PIECES = ("What a Code analysis report contains", "work out the languages, frameworks, data stores", "FINDINGS.md",
          "Severity (Critical, High, Medium or Low)", "Failure scenario", "Confidence (Confirmed, or Needs verification",
          "areas that could not be assessed", "release-risk assessment", "Skip style preferences and speculation")


class ReportContentsTests(unittest.TestCase):
    def test_delivery_and_reviewer_both_get_the_checklist_and_format(self):
        for role in ("engineering", "qa"):
            chapter = reference_chapters.role_chapters(role)["standing-checks"]
            for piece in PIECES:
                self.assertIn(piece, chapter, f"{role}: {piece}")


if __name__ == "__main__":
    unittest.main()
