# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Help says what the app now does (projects load one at a time, a Code analysis is described by project facts and answered by FINDINGS.md); the README names the kinds of work.

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

    def test_readme_names_the_kinds_of_work(self):
        # The README's "What's new" lists were removed (owner 2026-11-04); Help above still covers the rest.
        self.assertIn("Write code, Analyze code or Write specs", README)


if __name__ == "__main__":
    unittest.main()
