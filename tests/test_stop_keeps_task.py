# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Backlog #10 (owner's decision 2026-09-27): Stop closes the agent and keeps the task.

Stop on an unfinished Delivery task used to cancel it: records and workspace
removed, the task hidden (it lost the content-planning task on 2026-09-26).
Now Stop only closes the agent, the task stays on the board with everything it
had, the next Delivery agent the owner starts carries it on, Stop All cancels
nothing, and abandoning a task is its own explicit Cancel task action.
"""
from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from harness import board, board_viewer, control
from tests import test_board as _board_tests
from tests import test_board_viewer as _viewer_tests
from tests import test_owner_action_cards as _card_tests
from tests import test_reviewer_release as _release_tests
from tests.environment_support import require_loopback


class _Fixture(unittest.TestCase):
    setUp = _board_tests.BoardTests.setUp
    tearDown = _board_tests.BoardTests.tearDown
    ledger = _board_tests.BoardTests.ledger
    qa_command = _board_tests.BoardTests.qa_command
    declare_chunks = _board_tests.BoardTests.declare_chunks
    delivery = _board_tests.BoardTests.delivery
    atomic_plan = _board_tests.BoardTests.atomic_plan
    accept = _release_tests.AcceptanceTests.accept

    def working_task(self, task: str) -> dict:
        """A Delivery task with a plan, a brief, a finding and an open review."""
        dev = self.delivery(task)
        self.atomic_plan(dev["id"])
        board.task_brief(self.root, dev["id"], "Build the planner.", "Half way through the planner.")
        self.finding = board.record_finding(self.root, task, "A finding", "Belongs to this task", False)
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-" + task] = {
                "id": "review-" + task, "task": task, "developer_id": dev["id"],
                "status": "open", "requested_at": board.now(), "cycle": 1,
                "phase": "final_acceptance", "subtask": "", "chunk": "final",
                "claimed_by": None, "review_wait_started_at": board.now(),
            }
            state.setdefault("task_workspaces", {})[task] = str(self.root / ("workspace-" + task))
        return dev

    def new_delivery(self) -> dict:
        session = control.create(self.root, "codex_delivery")
        return board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])

    def stop(self, dev: dict) -> dict:
        result = board.stop_session(self.root, dev["session_id"])
        control.stop(self.root, dev["session_id"])  # what the Stop route does next
        return result

    def events(self, kind: str) -> list[dict]:
        return [event for event in board.snapshot(self.root)["events"] if event["kind"] == kind]


class StopKeepsTheTaskTests(_Fixture):
    """Criterion 1."""

    def test_stop_closes_the_agent_and_keeps_every_part_of_the_task(self):
        dev = self.working_task("PLANNER")
        before = board.snapshot(self.root)
        result = self.stop(dev)
        state = board.snapshot(self.root)
        self.assertEqual(result["kept_tasks"], ["PLANNER"])
        self.assertNotIn("cancelled_tasks", result)
        agent = state["agents"][dev["id"]]
        self.assertEqual((agent["active"], agent["status"], agent["write_authority"]), (False, "stopped", False))
        self.assertEqual(agent["status_note"], "Agent stopped. The task is kept — start a new agent to carry on.")
        self.assertNotIn("PLANNER", state.get("cancelled_tasks", {}))
        self.assertEqual(self.events("task_cancelled"), [])
        for key in ("delivery_plans", "task_briefs", "requirement_confirmations", "task_workspaces", "task_owner_directions"):
            self.assertEqual(state[key].get("PLANNER"), before[key].get("PLANNER"), key)
            self.assertIsNotNone(state[key].get("PLANNER"), key)
        self.assertIn("review-PLANNER", state["qa_requests"])
        self.assertIn(self.finding["id"], state["deferred_findings"])
        self.assertTrue((self.root / ".harness" / "tasks" / "PLANNER.json").is_file())
        self.assertEqual([e["task"] for e in self.events("delivery_stopped_task_kept")], ["PLANNER"])

    def test_the_stopped_task_stays_on_the_board_and_says_so(self):
        dev = self.working_task("PLANNER")
        self.stop(dev)
        payload = board_viewer.dashboard_payload(self.root)
        self.assertEqual(payload["live_tasks"], ["PLANNER"])
        self.assertEqual(payload["state"]["stopped_tasks"]["PLANNER"]["agent_id"], dev["id"])
        self.assertEqual(board_viewer.history_payload(self.root)["task_history"], [])

    def test_stopping_another_agent_on_the_task_leaves_the_delivery_running(self):
        dev = self.working_task("PLANNER")
        session = control.create(self.root, "claude_reviewer")
        board.register(self.root, "qa", "PLANNER", vendor="Anthropic", session_id=session["id"])
        result = board.stop_session(self.root, session["id"])
        self.assertEqual((result["related_session_ids"], result["kept_tasks"]), ([session["id"]], []))
        self.assertTrue(board.snapshot(self.root)["agents"][dev["id"]]["active"])


class NewAgentCarriesOnTests(_Fixture):
    """Criterion 2."""

    def test_a_new_delivery_agent_picks_up_the_stopped_task(self):
        dev = self.working_task("PLANNER")
        self.stop(dev)
        new = self.new_delivery()
        state = board.snapshot(self.root)
        self.assertEqual((new["task"], new["status"]), ("PLANNER", "recovered"))
        self.assertTrue(state["agents"][new["id"]]["write_authority"])
        self.assertEqual(state["agents"][dev["id"]]["superseded_by_agent_id"], new["id"])
        self.assertEqual([e["task"] for e in self.events("task_resumed")], ["PLANNER"])
        self.assertEqual(state["delivery_plans"]["PLANNER"]["mode"], "atomic")
        self.assertEqual(state["qa_requests"]["review-PLANNER"]["developer_id"], new["id"], "its open review follows it")
        texts = [item["text"] for item in control.take_instructions(self.root, new["session_id"])]
        self.assertTrue(any(text.startswith("Resume PLANNER.") for text in texts), texts)
        payload = board_viewer.dashboard_payload(self.root)
        self.assertEqual(payload["live_tasks"], ["PLANNER"])
        self.assertEqual(payload["state"]["stopped_tasks"], {})

    def test_the_next_agent_after_that_waits_for_direction(self):
        self.stop(self.working_task("PLANNER"))
        self.new_delivery()
        self.assertEqual(self.new_delivery()["task"], board.AWAITING_OWNER_DIRECTION)

    def test_two_stopped_tasks_are_carried_on_oldest_first(self):
        first = self.working_task("FIRST")
        second = self.working_task("SECOND")
        self.stop(first)
        self.stop(second)
        self.assertEqual(self.new_delivery()["task"], "FIRST")
        self.assertEqual(self.new_delivery()["task"], "SECOND")

    def test_a_release_waiting_for_the_owners_test_is_kept_but_not_handed_on(self):
        dev = self.working_task("RELEASED")
        with board.locked_state(self.root) as state:
            state.setdefault("releases", {})["RELEASED"] = {
                "task": "RELEASED", "status": "VISUAL_TEST_REQUIRED", "cto_id": "cto", "recorded_at": board.now(),
            }
        result = self.stop(dev)
        self.assertEqual(result["kept_tasks"], [])
        self.assertNotIn("RELEASED", board.snapshot(self.root).get("cancelled_tasks", {}))
        self.assertIn("RELEASED", board_viewer.dashboard_payload(self.root)["live_tasks"], "still waiting for the owner's test")
        self.assertEqual(self.new_delivery()["task"], board.AWAITING_OWNER_DIRECTION)

    def test_a_cancelled_or_accepted_task_is_never_carried_on(self):
        dev = self.working_task("GONE")
        self.stop(dev)
        board.cancel_task(self.root, "GONE")
        self.assertEqual(self.new_delivery()["task"], board.AWAITING_OWNER_DIRECTION)
        done = self.delivery("DONE")
        self.accept("DONE")
        self.stop(done)
        self.assertEqual(self.new_delivery()["task"], board.AWAITING_OWNER_DIRECTION)


class CancelTaskTests(_Fixture):
    """Criterion 3: only the explicit Cancel task cancels."""

    def test_cancel_task_removes_the_task_and_stops_its_agents(self):
        dev = self.working_task("PLANNER")
        result = board.cancel_task(self.root, "PLANNER")
        state = board.snapshot(self.root)
        self.assertEqual(result["cancelled_tasks"], ["PLANNER"])
        self.assertEqual(result["related_session_ids"], [dev["session_id"]])
        self.assertIn("PLANNER", state["cancelled_tasks"])
        self.assertEqual(state["agents"][dev["id"]]["status"], "cancelled")
        self.assertNotIn("review-PLANNER", state["qa_requests"])
        self.assertNotIn("PLANNER", state["delivery_plans"])
        self.assertNotIn(self.finding["id"], state["deferred_findings"])
        self.assertFalse((self.root / ".harness" / "tasks" / "PLANNER.json").exists())
        self.assertEqual(board_viewer.dashboard_payload(self.root)["live_tasks"], [])

    def test_a_stopped_task_can_still_be_cancelled(self):
        dev = self.working_task("PLANNER")
        self.stop(dev)
        board.cancel_task(self.root, "PLANNER")
        self.assertEqual(board_viewer.dashboard_payload(self.root)["live_tasks"], [])

    def test_cancel_task_refuses_what_it_must_not_touch(self):
        done = self.delivery("DONE")
        self.accept("DONE")
        with self.assertRaisesRegex(ValueError, "accepted and already in main"):
            board.cancel_task(self.root, "DONE")
        self.stop(done)  # frees a terminal slot for the next case
        self.delivery("WAITING")
        with board.locked_state(self.root) as state:
            state.setdefault("releases", {})["WAITING"] = {
                "task": "WAITING", "status": "VISUAL_TEST_REQUIRED", "cto_id": "cto", "recorded_at": board.now(),
            }
        with self.assertRaisesRegex(ValueError, "waiting for your test"):
            board.cancel_task(self.root, "WAITING")
        with self.assertRaisesRegex(ValueError, "unknown task"):
            board.cancel_task(self.root, "NOPE")
        self.delivery("TWICE")
        board.cancel_task(self.root, "TWICE")
        with self.assertRaisesRegex(ValueError, "already cancelled"):
            board.cancel_task(self.root, "TWICE")

    def test_the_cancel_route_cancels_and_stops_the_task_terminals(self):
        require_loopback()
        dev = self.working_task("PLANNER")
        with patch("harness.board_viewer.launch_terminal"):
            server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                post = lambda path: json.loads(urlopen(Request(base + path, data=b"{}", headers={"Content-Type": "application/json"}, method="POST"), timeout=5).read())
                response = post("/api/tasks/PLANNER/cancel")
                with self.assertRaises(HTTPError) as refused:
                    post("/api/tasks/PLANNER/cancel")
            finally:
                server.shutdown(); thread.join(timeout=3); server.server_close()
        self.assertEqual(response["cancelled_tasks"], ["PLANNER"])
        self.assertEqual([item["id"] for item in response["stopped_sessions"]], [dev["session_id"]])
        self.assertEqual(refused.exception.code, 400)
        self.assertEqual(control.snapshot(self.root)["active_counts"]["codex_delivery"], 0)


class StopAllTests(_Fixture):
    """Criterion 5."""

    def test_stop_all_stops_every_agent_and_cancels_no_task(self):
        first, second = self.working_task("FIRST"), self.working_task("SECOND")
        result = board.stop_all_sessions(self.root)
        state = board.snapshot(self.root)
        self.assertEqual(result["kept_tasks"], ["FIRST", "SECOND"])
        self.assertFalse(any(agent.get("active") for agent in state["agents"].values()))
        self.assertEqual(state.get("cancelled_tasks", {}), {})
        self.assertEqual(self.events("task_cancelled"), [])
        self.assertEqual(sorted(board_viewer.dashboard_payload(self.root)["live_tasks"]), ["FIRST", "SECOND"])
        self.assertEqual({first["id"], second["id"]}, {a["id"] for a in state["agents"].values() if a.get("stopped_by_owner_at")})


class UnchangedBehaviourTests(_Fixture):
    """Criterion 6: accepted-task Stop and the #9 card resolvers."""

    pin = _card_tests._Fixture.pin
    cto = _card_tests._Fixture.cto
    card = _card_tests._Fixture.card

    def test_accepted_task_stop_is_unchanged(self):
        dev = self.delivery("DONE")
        self.accept("DONE")
        result = self.stop(dev)
        self.assertEqual((result["accepted_tasks"], result["kept_tasks"]), (["DONE"], []))
        self.assertEqual([e["message"] for e in self.events("delivery_stopped_after_acceptance")], ["Task accepted, Dev agent stopped."])

    def test_stop_clears_agent_cards_and_keeps_task_cards_cancel_clears_task_cards(self):
        dev = self.working_task("PLANNER")
        agent_card = self.pin("Delivery is frozen. Stop it.", for_agent=dev["id"])
        task_card = self.pin("Answer the Delivery question on PLANNER.", task="PLANNER", kind="task")
        self.stop(dev)
        self.assertEqual(self.card(agent_card["id"])["outcome"], "Resolved: agent stopped")
        self.assertEqual(self.card(task_card["id"])["status"], "open", "the task is kept, so its card stays")
        board.cancel_task(self.root, "PLANNER")
        self.assertEqual(self.card(task_card["id"])["outcome"], "Resolved: task cancelled")


class PageWordingTests(unittest.TestCase):
    """Criterion 4 and the Cancel task button, run as page functions (node)."""

    script = _viewer_tests.BoardViewerTests.script
    declarations_only = _viewer_tests.BoardViewerTests.declarations_only
    run_node = _viewer_tests.BoardViewerTests.run_node

    HARNESS = """
const nodes={notice:{textContent:''}};
globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;}};
const calls=[];let prompt='';
globalThis.window={confirm(message){prompt=message;return true;}};
globalThis.fetch=async (path,options)=>{calls.push(path);return{ok:true,json:async()=>(%s)};};
refresh=async()=>{};
lastBoard={state:{}};
"""

    def run_page(self, response: dict, body: str) -> dict:
        return self.run_node(self.HARNESS % json.dumps(response) + body)

    def test_the_stop_prompt_and_notice_say_the_task_is_kept(self):
        result = self.run_page({"cleanup": {"kept_tasks": ["PLANNER"], "accepted_tasks": []}}, """
(async()=>{await confirmStopSession('codex-1','CODEX CLI · Delivery Agent','Planner','DEVELOPMENT IN PROGRESS','PLANNER');
process.stdout.write(JSON.stringify({prompt,notice:nodes.notice.textContent,calls}));})();""")
        self.assertIn("This closes the agent. The task is kept with all its work — start a new agent to carry on.", result["prompt"])
        self.assertIn("use Cancel task", result["prompt"])
        self.assertNotIn("removed", result["prompt"])
        self.assertEqual(result["notice"], "Agent stopped. The task is kept — start a new agent to carry on.")
        self.assertEqual(result["calls"], ["/api/sessions/codex-1/stop"])

    def test_stop_all_says_nothing_is_removed(self):
        result = self.run_page({"stopped_sessions": 3, "kept_tasks": ["A", "B"]}, """
(async()=>{await stopAllAgents();process.stdout.write(JSON.stringify({prompt,notice:nodes.notice.textContent}));})();""")
        self.assertIn("Every task is kept with all its work", result["prompt"])
        self.assertIn("Nothing is removed.", result["prompt"])
        self.assertEqual(result["notice"], "Stopped 3 terminals. Every task is kept — start a new agent to carry on.")

    def test_cancel_task_asks_plainly_and_calls_the_cancel_route(self):
        result = self.run_page({"cancelled_tasks": ["PLANNER"]}, """
(async()=>{const done=await confirmCancelTask('PLANNER');process.stdout.write(JSON.stringify({done,prompt,notice:nodes.notice.textContent,calls}));})();""")
        self.assertTrue(result["done"])
        self.assertIn("This abandons the task for good. Its board records and its workspace will be removed", result["prompt"])
        self.assertIn("use Stop instead", result["prompt"])
        self.assertEqual(result["calls"], ["/api/tasks/PLANNER/cancel"])
        self.assertIn("Task cancelled", result["notice"])

    def test_declining_cancel_task_changes_nothing(self):
        result = self.run_node("""
const calls=[];globalThis.window={confirm(){return false;}};globalThis.fetch=async p=>{calls.push(p);return{ok:true,json:async()=>({})};};
(async()=>{const done=await confirmCancelTask('PLANNER');process.stdout.write(JSON.stringify({done,calls}));})();""")
        self.assertEqual((result["done"], result["calls"]), (False, []))

    def test_the_button_is_offered_only_on_unfinished_work(self):
        result = self.run_node("""
process.stdout.write(JSON.stringify({
  open: cancelTaskHtml({}, 'OPEN'),
  released: cancelTaskHtml({releases:{R:{status:'VISUAL_TEST_REQUIRED'}}}, 'R'),
  accepted: cancelTaskHtml({release_decisions:{A:{decision:'accepted'}}}, 'A'),
}));""")
        self.assertIn(">Cancel task</button>", result["open"])
        self.assertEqual((result["released"], result["accepted"]), ("", ""))


if __name__ == "__main__":
    unittest.main()
