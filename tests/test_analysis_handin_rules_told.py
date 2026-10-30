# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The builder is told the Code analysis hand-in rules before it hands in, not after the board refuses it (2026-10-29 live run: three refusals)."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HandInRulesAreToldTests(unittest.TestCase):
    def test_the_report_paragraph_names_the_three_things_the_board_asks_for(self):
        text = " ".join((ROOT / "directives" / "AGENT.md").read_text(encoding="utf-8").split())
        start = text.index("What a Code analysis report contains.")
        paragraph = text[start:start + 6000]
        for piece in ("`Examined:` line", "`Proof:` line", "ONE plain command", "recognized runner", "no `&&`"):
            self.assertIn(piece, paragraph)


class MissingLibraryAdviceTests(unittest.TestCase):
    """2026-10-29 live run: 13 passing proof tests were refused with "No module named pytest" and no word on what to do."""

    def test_a_missing_library_says_what_to_do_and_other_failures_are_untouched(self):
        from harness import test_env
        advice = test_env.missing_library_advice("python3: No module named pytest")
        self.assertIn("test-env-set", advice)
        self.assertIn("not a failure of the tests themselves", advice)
        self.assertEqual(test_env.missing_library_advice("AssertionError: 1 != 2"), "")
        self.assertEqual(test_env.missing_library_advice(""), "")

    def test_both_places_that_run_a_stored_command_add_the_advice(self):
        root = Path(__file__).resolve().parents[1] / "harness"
        for name in ("board.py", "certified_execution.py"):
            self.assertIn("test_env.missing_library_advice(output)", (root / name).read_text(encoding="utf-8"), name)


if __name__ == "__main__":
    unittest.main()
