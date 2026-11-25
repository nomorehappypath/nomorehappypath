# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""board._saved_data_request_info end to end against a real Git repository: the UPGRADING.md
refusals (SAVED_DATA_UPGRADE_SAFETY.md 3.3.3), the "no flag means unchanged" default (3.2.1), and
the "rules" gate that keeps an unrelated task's final review untouched (section 10).
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


COMPLETE_UPGRADING = (
    "## What changes in your saved data\nEach to-do gains an empty due date.\n\n"
    "## Where your data is\ntodo.db; set TODO_DB for a test copy.\n\n"
    "## Before you update\nStop the app, copy todo.db, keep v1.\n\n"
    "## If something goes wrong\nStop the new app, put the copy back, start v1.\n"
)


class SavedDataRequestInfoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        _git(["init", "-q", "-b", "main"], self.workspace)
        _git(["config", "user.email", "harness@example.invalid"], self.workspace)
        _git(["config", "user.name", "Harness"], self.workspace)
        (self.workspace / "app.py").write_text("VERSION = 1\n")
        _git(["add", "."], self.workspace)
        _git(["commit", "-qm", "v1"], self.workspace)

    def _review_artifact(self, files: list[str]) -> dict:
        return {"files": files, "available": True}

    def test_no_declaration_at_all_defaults_to_unchanged_and_no_rules(self):
        info = board._saved_data_request_info(
            self.root, self.workspace, self._review_artifact(["app.py"]), "", "TASK DONE: YES\nSecurity check: fine.",
        )
        self.assertFalse(info["declared_changed"])
        self.assertFalse(info["rules"], "an ordinary task that never mentions saved data must not engage the rules")

    def test_explicit_unchanged_engages_the_rules_even_with_no_hint(self):
        info = board._saved_data_request_info(
            self.root, self.workspace, self._review_artifact(["app.py"]), "", "Saved data: unchanged. All good.",
        )
        self.assertFalse(info["declared_changed"])
        self.assertTrue(info["rules"])

    def test_declared_changed_without_upgrading_md_is_refused(self):
        with self.assertRaisesRegex(ValueError, "UPGRADING.md is missing"):
            board._saved_data_request_info(
                self.root, self.workspace, self._review_artifact(["app.py"]), "", "Saved data: changed — a due date",
            )

    def test_declared_changed_with_an_empty_heading_names_it(self):
        (self.workspace / "UPGRADING.md").write_text(COMPLETE_UPGRADING.replace(
            "## Before you update\nStop the app, copy todo.db, keep v1.\n\n", "## Before you update\n\n"))
        board._saved_data_request_info(
            self.root, self.workspace, self._review_artifact(["app.py", "UPGRADING.md"]), "", "Saved data: changed — a due date",
        )

    def test_declared_changed_with_a_complete_upgrading_md_succeeds(self):
        (self.workspace / "UPGRADING.md").write_text(COMPLETE_UPGRADING)
        info = board._saved_data_request_info(
            self.root, self.workspace, self._review_artifact(["app.py", "UPGRADING.md"]), "", "Saved data: changed — a due date",
        )
        self.assertTrue(info["declared_changed"])
        self.assertTrue(info["rules"])
        self.assertEqual(info["description"], "a due date")
        self.assertEqual(info["previous_state"], "known")

    def test_saved_data_flag_takes_priority_over_summary_and_reaches_the_description_verbatim(self):
        (self.workspace / "UPGRADING.md").write_text(COMPLETE_UPGRADING)
        raw = "Saved data: changed — the app now keeps wishes in its records file."
        info = board._saved_data_request_info(
            self.root, self.workspace, self._review_artifact(["app.py", "UPGRADING.md"]), "",
            "The app looks good.", saved_data=raw,
        )
        self.assertTrue(info["declared_changed"])
        self.assertEqual(info["description"], raw, "the flag's exact text must reach the brief untouched, not reparsed")

    def test_saved_data_flag_of_bare_unchanged_is_read_as_unchanged(self):
        info = board._saved_data_request_info(
            self.root, self.workspace, self._review_artifact(["app.py"]), "",
            "The app looks good.", saved_data="unchanged",
        )
        self.assertFalse(info["declared_changed"])
        self.assertTrue(info["rules"])

    def test_undeclared_but_hinted_migration_path_engages_the_rules(self):
        info = board._saved_data_request_info(
            self.root, self.workspace, self._review_artifact(["migrations/0002_due.py"]), "", "TASK DONE: YES\nSecurity check: fine.",
        )
        self.assertFalse(info["declared_changed"])
        self.assertTrue(info["rules"], "a hinted path must engage the rules even without a declaration")
        self.assertEqual(info["hint_paths"], ["migrations/0002_due.py"])
