# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Every finished step in the Story turns green (owner's order 2026-10-28).

Run:  PYTHONPATH=. python3 -m unittest tests.test_story_green_steps -v
"""
from __future__ import annotations

import unittest
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "harness" / "board_viewer.py").read_text(encoding="utf-8")


class GreenStepsTests(unittest.TestCase):
    def test_a_done_step_has_a_green_check_a_green_tag_and_a_green_line(self):
        for rule in (".mc-steps>li.mc-done::after", "background:#16a34a", "content:\"\\2713\"", ".mc-steps>li.mc-done .mc-st-tag", "color:#166534",
                     ".mc-steps>li.mc-done::before"):
            self.assertIn(rule, PAGE)

    def test_a_step_done_for_the_earlier_version_is_green_too_and_the_current_step_is_not(self):
        self.assertIn(".mc-steps>li.mc-earlier::after", PAGE)
        self.assertNotIn(".mc-steps>li.mc-now::after{background:#16a34a", PAGE)

    def test_the_row_carries_the_status_class_the_css_keys_on(self):
        self.assertIn('<li class="mc-${esc(row.status)}"', PAGE)


if __name__ == "__main__":
    unittest.main()
