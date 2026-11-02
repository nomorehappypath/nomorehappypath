# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Delivery directive runs the whole test suite once at the end, never in between (owner 2026-11-02)."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SuiteAtEndRuleTests(unittest.TestCase):
    def setUp(self):
        text = (ROOT / "directives" / "AGENT.md").read_text(encoding="utf-8")
        start = text.index("## delivery-workflow")
        self.chapter = text[start:text.index("\n## ", start + 5)]

    def test_the_whole_suite_runs_once_at_the_end_and_never_in_between(self):
        self.assertIn("The whole test suite runs once, at the end, and never in between", self.chapter)
        self.assertIn("run only the tests for the files you changed", self.chapter)
        self.assertIn("once, when the work is complete and just before you ask for review", self.chapter)

    def test_it_forbids_repeat_runs_and_baselines_on_main_and_gives_the_cheap_way_to_check(self):
        self.assertIn("never run it twice to\ncompare", self.chapter)
        self.assertIn("never run it on a clean copy of main to build a baseline", self.chapter)
        self.assertIn("rerun only those failing\ntests on a clean copy of main", self.chapter)
        self.assertIn("reported as not tested, never chased", self.chapter)


if __name__ == "__main__":
    unittest.main()
