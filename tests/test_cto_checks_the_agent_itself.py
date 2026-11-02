# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The CTO is told to look at the agent itself, not only at the board, before it calls an agent stuck (owner 2026-11-02)."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CtoChecksTheAgentTests(unittest.TestCase):
    def setUp(self):
        text = (ROOT / "directives" / "CTO.md").read_text(encoding="utf-8")
        start = text.index("## stalled")
        self.chapter = text[start:text.index("\n## ", start + 5)]

    def test_the_stalled_chapter_says_to_check_process_transcript_and_the_running_command(self):
        self.assertIn("Look at the agent itself before you say it is stuck", self.chapter)
        self.assertIn("`ps -p <pid>`", self.chapter)
        self.assertIn("control/transcripts", self.chapter)
        self.assertIn("waiting on its own running command is working", self.chapter)

    def test_it_says_when_it_is_stuck_and_asks_for_one_line_of_what_was_checked(self):
        self.assertIn("only when its process is gone", self.chapter)
        self.assertIn("Say in one line what you checked and what you found", self.chapter)

    def test_the_stalled_rules_and_the_new_check_do_not_contradict_each_other(self):
        self.assertIn("never describe it as working on the board's evidence alone", self.chapter)
        self.assertNotIn("never describe it as working.", self.chapter)
        self.assertIn("except when your own check shows the agent waiting on its own running command", self.chapter)


if __name__ == "__main__":
    unittest.main()
