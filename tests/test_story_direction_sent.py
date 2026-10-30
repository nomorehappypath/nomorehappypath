# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""After the owner sends a direction the Story must not say "Nothing is in progress, tell the builder" (2026-10-28).

Run:  PYTHONPATH=. python3 -m unittest tests.test_story_direction_sent -v
"""
from __future__ import annotations

import unittest
from pathlib import Path

from harness import story

PAGE = (Path(__file__).resolve().parents[1] / "harness" / "board_viewer.py").read_text(encoding="utf-8")


def state(text="TASK: do it", consumed=False, task="AWAITING_OWNER_DIRECTION", active=True):
    agent = {"id": "engineering-0001-aaaaaa", "role": "engineering", "task": task, "active": active, "session_id": "S1"}
    return {"agents": {agent["id"]: agent}, "owner_directions": {"S1": {"text": text, "consumed": consumed}} if text is not None else {}}


class DirectionSentTests(unittest.TestCase):
    def test_a_sent_direction_before_the_task_begins_is_reported(self):
        self.assertTrue(story._direction_sent(state(), None))

    def test_no_direction_means_the_start_button_stays(self):
        self.assertFalse(story._direction_sent(state(text=None), None))

    def test_a_consumed_direction_or_a_begun_task_is_not_pending(self):
        self.assertFalse(story._direction_sent(state(consumed=True), None))
        self.assertFalse(story._direction_sent(state(task="some-task"), None))

    def test_an_ended_agent_does_not_count(self):
        self.assertFalse(story._direction_sent(state(active=False), None))

    def test_the_page_says_so_and_drops_the_start_button(self):
        self.assertIn("The builder has your direction and is turning it into a plan.", PAGE)
        self.assertIn("!s.current&&s.direction_sent&&!s.exception", PAGE)

    def test_for_page_carries_the_flag(self):
        page = story.for_page(state(), {}, [])
        self.assertTrue(page["direction_sent"])
        self.assertFalse(story.for_page(state(text=None), {}, [])["direction_sent"])


if __name__ == "__main__":
    unittest.main()


class WaitForReviewTests(unittest.TestCase):
    def test_waiting_is_a_kept_visible_state_not_a_line_the_redraw_wipes(self):
        self.assertIn("const waitingForReview=new Set()", PAGE)
        self.assertIn("Waiting for the review…", PAGE)
        self.assertIn("data-waiting-note", PAGE)
        self.assertNotIn("Okay, nothing was changed. This card updates by itself", PAGE)
