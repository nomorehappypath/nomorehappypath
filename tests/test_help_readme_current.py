# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Help and the README say what the app now does: projects load one at a time, and a Code analysis is described by project facts and answered by FINDINGS.md.

Run:  PYTHONPATH=. python3 -m unittest tests.test_help_readme_current -v
"""
from __future__ import annotations

import unittest
from pathlib import Path

from harness.project_manager_page import PAGE

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


class HelpAndReadmeTests(unittest.TestCase):
    def test_help_explains_the_projects_list_and_the_analysis_report(self):
        for piece in ("Only the project that is open is loaded", "Not open. Open or resume it to see its progress.",
                      "FINDINGS.md", "Tell it only the facts about your project", "Takes the project off this list"):
            self.assertIn(piece, PAGE)

    def test_readme_lists_the_new_behaviour(self):
        for piece in ("Write code, Analyze code or Write specs", "Analyze code, builder and Reviewer in parallel", "Projects load one at a time",
                      "Delete on every project card", "FINDINGS.md"):
            self.assertIn(piece, README)


if __name__ == "__main__":
    unittest.main()
