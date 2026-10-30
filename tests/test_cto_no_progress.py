# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The CTO is woken by a free board check when a task makes no progress, not by a clock (2026-10-28_cto-pathfinder).

A task whose agents recorded no progress for the window wakes the CTO once with the reason; a healthy task never does; the slow
check-in is a once-a-day backstop; a route the CTO records reaches the stuck role's next poll.

Run:  PYTHONPATH=. python3 -m unittest tests.test_cto_no_progress -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from harness import board, control, cto_wakes
from tests.test_lighter_cto import _Fixture

WINDOW = cto_wakes.NO_PROGRESS_DEFAULT_SECONDS


class NoProgress(_Fixture):
    def setUp(self):
        super().setUp()
        cto_wakes.configure({})               # the real defaults: window 30 minutes, check-in once a day
        self.settle()

    def age_task(self, seconds):
        """The Delivery agent recorded nothing for `seconds`, yet it keeps polling (a polling loop is not progress)."""
        def older(value):
            return (datetime.fromisoformat(value) - timedelta(seconds=seconds)).isoformat() if value else value
        with board.locked_state(self.root) as state:
            agent = state["agents"][self.delivery["id"]]
            for key in ("last_progress_at", "last_status_at", "spawned_at"):
                agent[key] = older(agent.get(key))
        board.poll(self.root, self.delivery["id"])

    def test_a_stuck_task_wakes_the_cto_exactly_once_with_the_reason(self):
        self.age_task(WINDOW + 60)
        got = self.tick()
        self.assertEqual(len(got), 1)
        self.assertIn("a task with no progress", got[0]["text"])
        self.assertIn("APP", got[0]["text"])
        self.assertEqual(self.state()["cto_wakes"]["by_reason"]["no_progress"], 1)
        self.answer()
        for _ in range(3):                    # the same stall is one wake, polled or judged or not
            board.poll(self.root, self.delivery["id"])
            self.assertEqual(self.tick(), [])
        self.dispose([i for i in cto_wakes.open_items(self.state()) if i["reason"] == "no_progress"][0]["id"])
        self.assertEqual(self.tick(), [])

    def test_a_healthy_task_never_wakes_the_cto(self):
        self.age_task(WINDOW - 120)                       # inside the window
        self.assertEqual(self.tick(), [])
        board.status(self.root, self.delivery["id"], "wrote the first test", "working")
        self.age_task(WINDOW - 120)                       # a status post is progress and restarts the window
        self.assertEqual(self.tick(), [])
        self.assertNotIn("no_progress", self.state()["cto_wakes"]["by_reason"])

    def test_no_heartbeat_only_wake_inside_a_day(self):
        self.shift(3600 * 20)
        with board.locked_state(self.root) as state:      # the task itself stays healthy
            state["agents"][self.delivery["id"]]["last_progress_at"] = datetime.now(timezone.utc).isoformat()
        board.poll(self.root, self.cto["id"])
        self.assertEqual(self.tick(), [])
        self.shift(3600 * 5)
        with board.locked_state(self.root) as state:
            state["agents"][self.delivery["id"]]["last_progress_at"] = datetime.now(timezone.utc).isoformat()
        got = self.tick()
        self.assertEqual(len(got), 1)
        self.assertIn("the slow check-in", got[0]["text"])

    def test_a_paused_project_a_cancelled_task_and_a_task_in_review_are_not_stuck(self):
        self.age_task(WINDOW + 60)

        def stuck_items(change):
            copy = self.state()                           # judged on a copy: a paused board refuses writes
            change(copy)
            cto_wakes._no_progress(copy, datetime.now(timezone.utc))
            return [i for i in cto_wakes.open_items(copy) if i["reason"] == "no_progress"]

        self.assertEqual(len(stuck_items(lambda s: None)), 1, "the control case: this task is stuck")
        self.assertEqual(stuck_items(lambda s: s.update(project_pause={"status": "paused"})), [])
        self.assertEqual(stuck_items(lambda s: s["cancelled_tasks"].update(APP={"at": "now"})), [])
        self.assertEqual(stuck_items(lambda s: s["qa_requests"].update(R1={"task": "APP", "status": "claimed"})), [])

    def test_a_task_waiting_for_the_owner_is_not_stuck(self):
        self.age_task(WINDOW + 60)
        with board.locked_state(self.root) as state:
            state.setdefault("requirement_proposals", {})["APP"] = {"task": "APP", "status": "awaiting_owner"}
        self.assertEqual(self.tick(), [])

    def test_progress_then_a_second_stall_is_a_second_wake(self):
        self.age_task(WINDOW + 60)
        self.assertEqual(len(self.tick()), 1)
        self.answer()
        board.status(self.root, self.delivery["id"], "unblocked and moving", "working")
        self.assertEqual(self.tick(), [])
        self.age_task(WINDOW + 60)
        self.assertEqual(len(self.tick()), 1)

    def test_a_route_the_cto_records_reaches_the_stuck_agents_next_poll(self):
        self.age_task(WINDOW + 60)
        self.tick()
        self.answer()
        item = [i for i in cto_wakes.open_items(self.state()) if i["reason"] == "no_progress"][0]
        board.poll(self.root, self.delivery["id"])        # drain what the Delivery agent had already seen
        board.record_cto_disposition(self.root, self.cto["id"], item["id"], "route", "Run the build with --no-cache; the stale cache is the blocker.")
        seen = board.poll(self.root, self.delivery["id"])["events"]
        routed = [e for e in seen if e["kind"] == "cto_disposition_recorded"]
        self.assertEqual(len(routed), 1)
        self.assertIn("--no-cache", routed[0]["message"])

    def test_normal_quiet_states_never_wake_the_cto(self):
        """Round 1 of the review: an idle review queue, the owner's visual acceptance and a Delivery agent awaiting direction are normal."""
        self.age_task(WINDOW - 600)                       # Delivery is healthy
        reviewer_session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=reviewer_session["id"])
        with board.locked_state(self.root) as state:      # the idle Reviewer has recorded nothing for longer than the window
            for key in ("last_progress_at", "last_status_at", "spawned_at"):
                value = state["agents"][reviewer["id"]].get(key)
                if value:
                    state["agents"][reviewer["id"]][key] = (datetime.fromisoformat(value) - timedelta(seconds=WINDOW + 600)).isoformat()
        board.poll(self.root, reviewer["id"])
        self.assertEqual(self.tick(), [], "an idle review queue is not a stuck task")
        self.age_task(WINDOW + 60)

        def stuck_items(change):
            copy = self.state()
            change(copy)
            cto_wakes._no_progress(copy, datetime.now(timezone.utc))
            return [i for i in cto_wakes.open_items(copy) if i["reason"] == "no_progress"]

        self.assertEqual(len(stuck_items(lambda s: None)), 1, "the control case: the Delivery task is stuck")
        self.assertEqual(stuck_items(lambda s: s.setdefault("releases", {}).update(APP={"status": "VISUAL_TEST_REQUIRED"})), [],
                         "released and waiting for the owner's visual acceptance")
        self.assertEqual(stuck_items(lambda s: s["agents"][self.delivery["id"]].update(task=board.AWAITING_OWNER_DIRECTION)), [])

    def test_a_long_route_note_reaches_the_stuck_agent_complete(self):
        self.age_task(WINDOW + 60)
        self.tick()
        self.answer()
        item = [i for i in cto_wakes.open_items(self.state()) if i["reason"] == "no_progress"][0]
        board.poll(self.root, self.delivery["id"])
        command = "PYTHONPATH=. python3 -m unittest tests.test_the_stuck_one -v"
        note = ("The build keeps failing on a stale cache from the earlier attempt, so clear it first and rebuild from scratch. " * 3) + "Then run: " + command
        self.assertGreater(len(note), 300)
        board.record_cto_disposition(self.root, self.cto["id"], item["id"], "route", note)
        seen = board.poll(self.root, self.delivery["id"])["events"]
        routed = [e for e in seen if e["kind"] == "cto_disposition_recorded"]
        self.assertEqual(len(routed), 1)
        self.assertTrue(routed[0]["route_note"].endswith(command), routed[0]["route_note"][-80:])


if __name__ == "__main__":
    unittest.main()
