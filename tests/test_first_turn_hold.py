# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A fresh Claude terminal takes controller messages only after its first turn (2026-10-28: a direction typed at start-up was lost).

Run:  PYTHONPATH=. python3 -m unittest tests.test_first_turn_hold -v
"""
from __future__ import annotations

import unittest

from harness import interactive_supervisor as sup


class FirstTurnSettledTests(unittest.TestCase):
    def test_held_while_the_first_turn_is_running(self):
        self.assertFalse(sup._first_turn_settled("working", 105.0, 100.0, 110.0))

    def test_held_when_only_an_earlier_launch_was_idle(self):
        self.assertFalse(sup._first_turn_settled("idle_at_prompt", 90.0, 100.0, 110.0))

    def test_held_when_no_signal_has_arrived_yet(self):
        self.assertFalse(sup._first_turn_settled("", 0.0, 100.0, 105.0))

    def test_released_when_the_first_turn_is_idle_after_this_launch(self):
        self.assertTrue(sup._first_turn_settled("idle_at_prompt", 125.0, 100.0, 126.0))

    def test_never_held_for_ever_without_a_signal(self):
        self.assertTrue(sup._first_turn_settled("", 0.0, 100.0, 100.0 + sup.FIRST_TURN_HOLD_SECONDS))


class FreshLaunchTests(unittest.TestCase):
    def fresh(self, session):
        from unittest import mock
        with mock.patch.object(sup.control, "_read_state", return_value={"sessions": {"S": session}}):
            return sup._fresh_launch(None, "S")

    def test_a_first_launch_is_fresh(self):
        self.assertTrue(self.fresh({"cli_launches": 1, "cli_last_launch_resumed": False}))

    def test_a_resume_is_not_fresh(self):
        self.assertFalse(self.fresh({"cli_launches": 2, "cli_last_launch_resumed": True}))

    def test_a_session_with_no_recorded_launch_is_not_held(self):
        self.assertFalse(self.fresh({}))


if __name__ == "__main__":
    unittest.main()
