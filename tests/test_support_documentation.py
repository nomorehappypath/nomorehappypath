# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""R1: the README's Support chapter and the Help page's Support row tell the owner's ten steps,
in order, with the flow picture and what Support never does — delivered before any code, so this
is the documentation this task's own contract check runs against.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from harness.project_manager_page import PAGE


class SupportChapterTests(unittest.TestCase):
    def test_support_chapter_covers_the_ten_steps_in_order_and_what_it_never_does(self):
        readme = Path("README.md").read_text()
        self.assertIn("## Support", readme)
        chapter = readme.split("## Support", 1)[1].split("\n## ", 1)[0].lower()
        steps = [
            "build", "second", "error_sink_url", "received", "reported version",
            "repair", "pull request", "deploy", "support_plan.md",
        ]
        positions = [chapter.find(step) for step in steps]
        self.assertTrue(all(position >= 0 for position in positions), positions)
        self.assertEqual(positions, sorted(positions), "the steps must read in the owner's own order")
        for phrase in ("never logs into the app server", "never holds trading", "never deploys by itself"):
            self.assertIn(phrase, chapter)
        self.assertIn("app server", chapter)
        self.assertIn("→", chapter, "the flow picture, drawn as arrows")
        # Review r5 B7: the owner key is chosen on first visit, never printed by Support itself.
        self.assertNotIn("shown once where support was started", chapter)
        self.assertIn("choose your own owner key", chapter)
        self.assertIn("12 characters", chapter)

    def test_help_mentions_support(self):
        self.assertIn("Support", PAGE)


if __name__ == "__main__":
    unittest.main()
