# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The stranger run of 2026-11-05 (a fresh install building a weather app with no hands on it) stopped for four hours:

the builder had finished and asked for review; the board ran the Reviewer's checks with plain Python, which had no Streamlit,
and told the builder "the CTO prepares the test environment"; the builder was not allowed to declare one, posted a blocked
status with a question for the CTO, and nothing carried that question to the CTO (it had closed the earlier item and slept);
meanwhile the stall ladder woke the builder and the Reviewer every five minutes, 82 times each, each answered by one poll
and nothing new, for about five dollars.

Three rules, tested here:
  * Delivery declares the project's test environment itself (it built it), and every message says so;
  * a blocked Delivery is a stuck item for the CTO (cto_wakes.EVENT_REASONS["delivery_blocked"]);
  * two wake-ups answered with nothing new at the same gate end the ladder: the builder's stall pauses the project with one
    card naming its last word and the cost; a quiet Reviewer is left alone (owner 2026-11-05: no wake loops).

Run:  PYTHONPATH=. python3 -m unittest tests.test_stranger_run_stall -v
"""
import unittest

from harness import board, board_surface, control, cto_wakes, review_rounds, self_heal, test_env
from tests.test_no_silent_stall import _Stall
from tests.test_self_restart import _ago


class DeliveryDeclaresTheTestEnvironment(unittest.TestCase):
    def test_delivery_may_declare_and_every_message_names_delivery(self):
        self.assertIn("test-env-set", board_surface.DELIVERY_OPERATIONS)
        self.assertIn("test-env-set", board_surface.CTO_OPERATIONS)
        self.assertNotIn("test-env-set", board_surface.REVIEWER_OPERATIONS)
        advice = test_env.missing_library_advice("ModuleNotFoundError: No module named 'streamlit'")
        self.assertIn("Delivery prepares it", advice)
        self.assertIn("test-env-set", advice)
        self.assertNotIn("CTO", advice)
        self.assertNotIn("CTO", review_rounds.ENVIRONMENT_GAP_MESSAGE)
        self.assertIn("Delivery prepares", review_rounds.ENVIRONMENT_GAP_MESSAGE)
        self.assertNotIn("ask the CTO", test_env.HOW_TO_PREPARE)


class ABlockedDeliveryIsAStuckItem(_Stall):
    def test_a_blocked_status_opens_one_cto_item_with_the_builder_s_words(self):
        _, agent, _ = self.delivery_with_task("weather-app")
        self.cto()
        note = "Route question: review refused, plain Python lacks Streamlit. Please declare a Python with Streamlit."
        board.status(self.root, agent["id"], note, "blocked")
        stuck = [item for item in cto_wakes.open_items(board.snapshot(self.root))
                 if item["reason"] == "stuck" and item["subject"] == "delivery_blocked:weather-app"]
        self.assertEqual(len(stuck), 1)
        self.assertIn("Route question", stuck[0]["detail"])
        self.assertTrue(stuck[0]["wake_pending"])
        board.status(self.root, agent["id"], note + " (still waiting)", "blocked")
        again = [item for item in cto_wakes.open_items(board.snapshot(self.root)) if item["subject"] == "delivery_blocked:weather-app"]
        self.assertEqual(len(again), 1, "the same block never opens a second item")
        self.assertEqual(again[0]["repeats"], 1)

    def test_a_working_status_opens_nothing(self):
        _, agent, _ = self.delivery_with_task("weather-app")
        self.cto()
        board.status(self.root, agent["id"], "writing the app", "working")
        self.assertEqual([i for i in cto_wakes.open_items(board.snapshot(self.root)) if i["reason"] == "stuck"], [])


class TwoEmptyWakesEndTheLadder(_Stall):
    def quiet_again(self, agent_id: str) -> None:
        """The agent answered the wake with one poll and nothing new; the window passes again."""
        with board.locked_state(self.root, allow_paused=True, operation="test clock") as state:
            state["agents"][agent_id]["last_poll_at"] = _ago(self.window + 60)

    def actions(self) -> list[str]:
        return [r["action"] for r in self_heal.run_once(self.root, self.launch, create=self.create)]

    def test_the_builder_s_stall_pauses_the_project_with_one_card(self):
        session, agent, _ = self.delivery_with_task("weather-app")
        board.status(self.root, agent["id"], "Route question: the review needs a Python with Streamlit.", "blocked")
        self.silent(agent["id"], self.window + 60)
        self.assertEqual(self.actions(), ["stall_wake"])
        board.poll(self.root, agent["id"])                      # one poll, nothing new
        self.assertEqual(self.actions(), ["stall_reset"])
        self.quiet_again(agent["id"])
        self.assertEqual(self.actions(), ["stall_wake"])
        board.poll(self.root, agent["id"])
        self.assertEqual(self.actions(), ["stall_reset"])
        self.quiet_again(agent["id"])
        self.assertEqual(self.actions(), ["stall_pause"])
        self.assertEqual(board.pause_state(self.root)["status"], "paused")
        cards = self.cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("is stuck: the project is paused to stop spending", cards[0]["title"])
        self.assertIn("Route question", cards[0]["why"])
        paused = self.events("stall_paused")
        self.assertEqual(len(paused), 1)
        self.assertIn("answered 2 wake-ups with nothing new", paused[0]["message"])
        self.assertIn("press Resume", paused[0]["message"])
        self.assertEqual(sum(1 for e in self.events("agent_stall_step") if e.get("step") == "wake"), 2, "no third wake")
        self.quiet_again(agent["id"])
        self.assertNotIn("stall_wake", self.actions(), "a paused project is not woken")
        self.assertEqual(len(self.cards()), 1, "one card, never a second")

    def test_a_quiet_reviewer_is_left_alone_not_paused(self):
        _, agent, _ = self.reviewer("weather-app")
        self.silent(agent["id"], self.window + 60)
        self.assertEqual(self.actions(), ["stall_wake"])
        board.poll(self.root, agent["id"])
        self.assertEqual(self.actions(), ["stall_reset"])
        self.quiet_again(agent["id"])
        self.assertEqual(self.actions(), ["stall_wake"])
        board.poll(self.root, agent["id"])
        self.assertEqual(self.actions(), ["stall_reset"])
        self.quiet_again(agent["id"])
        self.assertEqual(self.actions(), ["stall_stop_waking"])
        self.assertEqual(board.pause_state(self.root)["status"], "active")
        self.assertEqual(self.cards(), [])
        stopped = [e for e in self.events("agent_stall_step") if e.get("step") == "stop_waking"]
        self.assertEqual(len(stopped), 1)
        self.assertIn("stops waking it", stopped[0]["message"])
        self.quiet_again(agent["id"])
        self.assertEqual(self.actions(), [], "nothing more while nothing new is routed to it")

    def test_progress_starts_the_count_over(self):
        _, agent, _ = self.delivery_with_task("weather-app")
        self.silent(agent["id"], self.window + 60)
        self.assertEqual(self.actions(), ["stall_wake"])
        board.poll(self.root, agent["id"])
        self.assertEqual(self.actions(), ["stall_reset"])
        self.quiet_again(agent["id"])
        self.assertEqual(self.actions(), ["stall_wake"])
        board.status(self.root, agent["id"], "the environment is declared; asking for the review", "working")   # progress: a new gate
        self.assertEqual(self.actions(), ["stall_reset"])
        with board.locked_state(self.root) as state:
            stamp = _ago(self.window + 60)
            state["agents"][agent["id"]].update({"last_poll_at": stamp, "last_progress_at": stamp})
        self.assertEqual(self.actions(), ["stall_wake"], "a moved gate is a fresh count: woken, not paused")
        self.assertEqual(board.pause_state(self.root)["status"], "active")


if __name__ == "__main__":
    unittest.main()
