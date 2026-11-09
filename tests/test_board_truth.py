# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The board tells the owner the truth about who is there (task 2026-11-08_accept-after-repair, R6 and R7).

R6: a question from an agent that is gone (3 November, task closed, agent long superseded) showed for five days as "An agent
has a question for you". R7: the "nobody is working" card fired about a builder whose window was open, with the words
"is gone: board heartbeat is current"; and the owner's Try once more reached the builder only when the CTO relayed it."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from harness import board, story

TASK = "finish-support"


def _ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", session_id="cto-session")
        self.builder = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="builder-session")
        with board.locked_state(self.root, operation="test") as state:
            state["agents"][self.builder["id"]].update({"task": TASK, "active": True})
            state.setdefault("task_owner_directions", {})[TASK] = "Finish Support."

    def state(self):
        return board.snapshot(self.root)

    def agent_update(self, **fields):
        with board.locked_state(self.root, operation="test") as state:
            state["agents"][self.builder["id"]].update(fields)

    def open_cards(self):
        return [c for c in self.state().get("owner_actions", {}).values() if c["status"] == "open"]


class R6DeadQuestionsExpire(Case):
    def ask(self):
        return board.record_agent_question(self.root, self.builder["id"], "Should the guide name the key?", ["Yes", "No"])

    def test_a_gone_agents_question_on_a_closed_task_leaves_the_page(self):
        question = self.ask()
        self.assertEqual(story.project_now(self.state(), TASK)["needs_you"]["kind"], "question")
        self.agent_update(active=False, status="superseded")
        with board.locked_state(self.root, operation="test") as state:
            state.setdefault("cancelled_tasks", {})[TASK] = {"cancelled_at": _ago(1)}
        board.harness_cards_pass(self.root)
        entry = self.state()["agent_questions"][question["id"]]
        self.assertEqual(entry["status"], "expired")
        self.assertNotEqual((story.project_now(self.state(), TASK).get("needs_you") or {}).get("kind"), "question")
        self.assertIn("agent_question_expired", [e["kind"] for e in self.state()["events"]])
        with self.assertRaisesRegex(ValueError, "not open"):
            board.record_agent_question_answer(self.root, question["id"], "Yes")

    def test_a_paused_projects_question_is_kept_and_a_live_agents_question_is_untouched(self):
        question = self.ask()
        self.agent_update(active=False, status="paused")
        with board.locked_state(self.root, operation="test") as state:
            state["project_pause"] = {"status": "paused", "pause_id": "p", "agents": {self.builder["id"]: {"session_id": "builder-session", "task": TASK}}}
        self.assertFalse(board._question_asker_gone(self.state(), self.state()["agent_questions"][question["id"]]), "paused is not gone")
        with board.locked_state(self.root, allow_paused=True, operation="test") as state:
            state["project_pause"] = {"status": "active"}
            state["agents"][self.builder["id"]].update({"active": True, "status": "working"})
        board.harness_cards_pass(self.root)
        self.assertEqual(self.state()["agent_questions"][question["id"]]["status"], "open")

    def test_answering_is_refused_under_the_lock_when_the_asker_left_meanwhile(self):
        question = self.ask()
        self.agent_update(active=False, status="stopped", stopped_by_owner_at=_ago(1))
        with self.assertRaisesRegex(ValueError, "gone"):
            board.record_agent_question_answer(self.root, question["id"], "Yes")
        self.assertEqual(self.state()["agent_questions"][question["id"]]["status"], "expired")


class R7IdleCardTruthAndTheWake(Case):
    def test_a_status_update_counts_as_seen_and_a_live_window_is_never_called_gone(self):
        self.agent_update(last_poll_at=_ago(12), last_status_at=_ago(3))
        self.assertEqual(board.harness_cards_pass(self.root), [], "a status update three minutes ago is a sign of life")
        self.agent_update(spawned_at=_ago(40), last_poll_at=_ago(12), last_status_at=_ago(12), liveness="healthy", liveness_note="board heartbeat is current")
        with mock.patch("harness.control.live_session_ids", return_value={"builder-session"}):
            cards = board.harness_cards_pass(self.root)
        self.assertEqual(len(cards), 1)
        self.assertIn("still has its window open but has not checked the board", cards[0]["why"])
        self.assertNotIn("is gone", cards[0]["why"])
        self.assertNotIn("heartbeat is current", cards[0]["why"])
        self.assertIn("Stop that builder", cards[0]["why"])
        self.assertFalse(self.state()["agents"][self.builder["id"]].get("stopped_by_harness_at"), "a live builder's task is not handed away under it")

    def test_a_dead_window_keeps_the_hand_over_marker_and_honest_words(self):
        self.agent_update(active=False, status="superseded", liveness="offline", liveness_note="visible CLI terminal ended", last_poll_at=_ago(12), last_status_at=_ago(12))
        with mock.patch("harness.control.live_session_ids", return_value=set()):
            cards = board.harness_cards_pass(self.root)
        self.assertEqual(len(cards), 1)
        self.assertIn("is gone: visible CLI terminal ended", cards[0]["why"])
        self.assertTrue(self.state()["agents"][self.builder["id"]].get("stopped_by_harness_at"))

    def stopped_part(self):
        with board.locked_state(self.root, operation="test") as state:
            entry = {"key": f"{TASK}|final", "task": TASK, "scope": {"phase": "final_acceptance"}, "status": "could_not_finish",
                     "failed_judgments": 5, "open_ids": ["B2"], "outcome": "Could not finish: B2 still failed.", "settled_at": _ago(2)}
            state.setdefault("review_escalations", {})[entry["key"]] = entry
        cards = board.harness_cards_pass(self.root)
        return next(c for c in cards if c.get("choice_kind") == "review_stop")

    def test_try_once_more_types_the_choice_into_the_builders_window(self):
        card = self.stopped_part()
        with mock.patch("harness.control.enqueue_instruction", return_value={"id": "queued-1"}) as enqueue:
            result = board.owner_action_choose(self.root, card["id"], "Try once more")
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], "builder-session")
        self.assertIn("Try once more", enqueue.call_args.args[2])
        entry = self.state()["review_escalations"][f"{TASK}|final"]
        self.assertEqual(entry["pending_owner_choice"]["queued"], "queued-1")
        self.assertEqual(entry["status"], "watching")
        self.assertNotIn("could not be reached", result["message"])

    def test_an_unreachable_window_is_said_on_the_card_not_hidden(self):
        card = self.stopped_part()
        with mock.patch("harness.control.enqueue_instruction", side_effect=ValueError("cannot route to an inactive managed session")):
            result = board.owner_action_choose(self.root, card["id"], "Try once more")
        self.assertIn("could not be reached", result["message"])
        self.assertIn("start a new builder", result["message"])
        self.assertEqual(self.state()["review_escalations"][f"{TASK}|final"]["pending_owner_choice"]["queued"], "")


if __name__ == "__main__":
    unittest.main()
