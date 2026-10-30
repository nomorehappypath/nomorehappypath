# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Story screen carries Cancel this task (2026-10-28: the owner could not find it once the Story replaced the old task cards).

Run:  PYTHONPATH=. python3 -m unittest tests.test_story_cancel_button -v
"""
from __future__ import annotations

import unittest
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "harness" / "board_viewer.py").read_text(encoding="utf-8")


class StoryCancelTests(unittest.TestCase):
    def test_the_now_card_and_the_other_tasks_render_a_cancel_button(self):
        self.assertIn("storyCancelButton(c.task,'Cancel this task')", PAGE)
        self.assertIn("storyCancelButton(o.task,'Cancel')", PAGE)

    def test_the_click_goes_through_the_one_confirmed_cancel(self):
        self.assertIn("confirmCancelTask(cancel.dataset.mcCancel)", PAGE)

    def test_no_task_means_no_button(self):
        self.assertIn("return task?`<button", PAGE)


if __name__ == "__main__":
    unittest.main()
