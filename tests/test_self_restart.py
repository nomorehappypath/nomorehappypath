# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Backlog #7 (owner feature): the harness restarts stuck agents by itself.

"i cannot afford being abscent for hours and come back and see you guys stuck
waiting for me" (owner, 2026-09-26). Real processes stand in for terminals; the
launcher is a fake that records what the harness would open.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance, control, project_memory, project_worker, self_heal
from tests.requirements_support import agreed_requirements

NOW = datetime.now(timezone.utc)


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.processes: list[subprocess.Popen] = []
        self.addCleanup(self._reap)
        self.launched: list[dict] = []
        self.created: list[str] = []

    def _reap(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def stand_in(self) -> subprocess.Popen:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
        self.processes.append(process)
        return process

    def launch(self, session: dict) -> None:
        self.launched.append(session)

    def create(self, kind: str) -> dict:
        self.created.append(kind)
        return control.create(self.root, kind)

    def delivery_with_task(self, task: str = "BRIEF-TASK") -> tuple[dict, dict, subprocess.Popen]:
        session = control.create(self.root, "codex_delivery")
        process = self.stand_in()
        control.attach(self.root, session["id"], process.pid)
        agent = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], f"OWNER DIRECTION — {task}")
        board.begin_task(self.root, agent["id"], task)
        agreed_requirements(self.root, agent["id"], f"Final agreed requirements for {task}: deliver it.")
        return session, board.snapshot(self.root)["agents"][agent["id"]], process

    def freeze(self, session_id: str, minutes: float = 16) -> None:
        control.enqueue_instruction(self.root, session_id, "OWNER CLARIFICATION: is the brief ready?", "owner-message")
        with control.locked_state(self.root) as state:
            for entry in state["inbox"][session_id]:
                entry["queued_at"] = _ago(minutes * 60)
            state["sessions"][session_id]["last_output_at"] = _ago(minutes * 60)


class DecisionTests(_Fixture):
    def test_a_frozen_terminal_with_waiting_messages_is_restarted(self):
        session, agent, _ = self.delivery_with_task()
        self.freeze(session["id"])
        actions = self_heal.plan(self.root)
        self.assertEqual([(a["action"], a["session_id"], a["agent_id"]) for a in actions],
                         [("restart", session["id"], agent["id"])])
        self.assertIn("showed nothing for 16 minutes", actions[0]["reason"])

    def test_a_quiet_terminal_is_not_frozen_while_it_is_still_talking_or_has_nothing_queued(self):
        session, _, _ = self.delivery_with_task()
        control.enqueue_instruction(self.root, session["id"], "a message", "owner-message")
        with control.locked_state(self.root) as state:
            state["inbox"][session["id"]][0]["queued_at"] = _ago(20 * 60)
            state["sessions"][session["id"]]["last_output_at"] = _ago(60)   # it spoke a minute ago
        self.assertEqual(self_heal.plan(self.root), [])
        other, _, _ = self.delivery_with_task("OTHER-TASK")
        with control.locked_state(self.root) as state:
            state["sessions"][other["id"]]["last_output_at"] = _ago(3600)   # silent, but nothing is waiting
            state["inbox"].pop(session["id"], None)
        self.assertEqual(self_heal.plan(self.root), [])

    def test_sign_in_paused_and_superseded_terminals_are_never_touched(self):
        session, _, _ = self.delivery_with_task()
        self.freeze(session["id"])
        control.record_attention(self.root, session["id"], "Please run /login · API Error: 401")
        self.assertEqual(self_heal.plan(self.root), [], "sign-in is the owner's")
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["attention_reason"] = None
            state["sessions"][session["id"]]["superseded_by_session_id"] = "newer"
        self.assertEqual(self_heal.plan(self.root), [], "a superseded terminal is read-only history")
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]].pop("superseded_by_session_id")
        with board.locked_state(self.root) as state:
            state["project_pause"] = {"status": "paused"}
        self.assertEqual(self_heal.plan(self.root), [], "a paused project is left alone")

    def test_a_terminal_that_ended_by_itself_mid_task_is_brought_back_only_if_recent_and_not_replaced(self):
        session, agent, process = self.delivery_with_task()
        process.kill(); process.wait(timeout=5)
        board.offline(self.root, agent["id"], "visible CLI terminal ended", transport_ended=True)
        actions = self_heal.plan(self.root)
        self.assertEqual([(a["action"], a["session_id"]) for a in actions], [("relaunch_ended", session["id"])])
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["ended_at"] = _ago(2 * 3600)
        self.assertEqual(self_heal.plan(self.root), [], "an old ended session is history")

    def test_a_terminal_the_owner_stopped_is_never_brought_back(self):
        session, agent, _ = self.delivery_with_task()
        control.stop(self.root, session["id"])
        time.sleep(0.3)
        board.offline(self.root, agent["id"], "visible CLI terminal ended", transport_ended=True)
        self.assertEqual(self_heal.plan(self.root), [])

    def test_a_missing_reviewer_is_started_but_not_while_claude_needs_sign_in(self):
        with board.locked_state(self.root) as state:
            state["reviewer_needed"] = {"requested_at": _ago(6 * 60), "request_id": "review-1"}
        actions = self_heal.plan(self.root)
        self.assertEqual([a["action"] for a in actions], ["start_reviewer"])
        cto = control.create(self.root, "claude_cto")
        control.attach(self.root, cto["id"], self.stand_in().pid)
        control.record_attention(self.root, cto["id"], "Login expired - Please run /login")
        self.assertEqual(self_heal.plan(self.root), [], "a new Reviewer would be signed out too")

    def test_three_restarts_in_an_hour_then_one_plain_give_up_line(self):
        session, agent, _ = self.delivery_with_task()
        self.freeze(session["id"])
        with board.locked_state(self.root) as state:
            state.setdefault("self_heal", {}).setdefault("attempts", {})[f"agent:{agent['id']}"] = [
                _ago(60) for _ in range(self_heal.MAX_ACTIONS_PER_HOUR)]
        actions = self_heal.plan(self.root)
        self.assertEqual([a["action"] for a in actions], ["give_up"])
        self_heal.execute(self.root, actions[0], self.launch)
        gave_up = [e for e in board.snapshot(self.root)["events"] if e["kind"] == "self_heal_gave_up"]
        self.assertEqual(len(gave_up), 1)
        self.assertIn("still stuck", gave_up[0]["message"])
        self.assertIn("open its terminal", gave_up[0]["message"])
        self.assertEqual(self_heal.plan(self.root), [], "it says so once, then stays quiet")


class TwoPhaseRestartTests(_Fixture):
    def test_the_same_agent_comes_back_with_its_task(self):
        session, agent, process = self.delivery_with_task("KEEP-THIS-TASK")
        self.freeze(session["id"])
        first = self_heal.run_once(self.root, self.launch, create=self.create)
        self.assertEqual(first[0]["phase"], "stopping")
        process.wait(timeout=5)   # control.stop signalled the stand-in terminal
        board.offline(self.root, agent["id"], "visible CLI terminal ended", transport_ended=True)   # what the supervisor does on exit
        state = board.snapshot(self.root)
        self.assertNotIn("KEEP-THIS-TASK", state.get("cancelled_tasks", {}), "a restart never cancels the task")
        second = self_heal.run_once(self.root, self.launch, create=self.create)
        self.assertEqual(second, [{"action": "relaunch", "session_id": session["id"], "phase": "launched"}])
        self.assertEqual([item["id"] for item in self.launched], [session["id"]], "the SAME session is relaunched")
        # the runner registers again for that session, as run_managed_agent.sh does
        back = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        self.assertEqual(back["id"], agent["id"], "the SAME agent is reattached, not a new empty one")
        self.assertEqual(back["task"], "KEEP-THIS-TASK")
        self.assertTrue(back["active"])
        kinds = [event["kind"] for event in board.snapshot(self.root)["events"]]
        self.assertIn("agent_restart_requested", kinds)
        self.assertIn("agent_restarted_by_harness", kinds)
        self.assertEqual(self_heal.run_once(self.root, self.launch, create=self.create), [], "nothing more to do")

    def test_a_missing_reviewer_is_started_with_the_owners_settings_path(self):
        with board.locked_state(self.root) as state:
            state["reviewer_needed"] = {"requested_at": _ago(6 * 60), "request_id": "review-1"}
        results = self_heal.run_once(self.root, self.launch, create=self.create)
        self.assertEqual(self.created, ["claude_reviewer"], "created through the worker's own creation path")
        self.assertEqual(len(self.launched), 1)
        started = [e for e in board.snapshot(self.root)["events"] if e["kind"] == "reviewer_started_by_harness"]
        self.assertIn("The harness started a Reviewer because a review has waited 6 minutes", started[0]["message"])
        self.assertEqual(results[0]["action"], "start_reviewer")


class FailingLauncherTests(_Fixture):
    """Review round 1: a launcher that always fails must not bypass the cap.

    The reviewer's reproduction: five passes with a failing launcher created five
    failed Reviewer sessions and no give-up line. For every path that opens a
    terminal, many watchdog passes must give at most MAX_ACTIONS_PER_HOUR
    attempts, then exactly one plain give-up line, then silence."""

    PASSES = 8

    def failing_launch(self, session: dict) -> None:
        """Fails exactly as the worker's launcher does: the session is marked failed, then it raises."""
        self.launched.append(session)
        control.fail_launch(self.root, session["id"], "the harness could not reopen the terminal: Terminal could not be opened")
        raise RuntimeError("Terminal could not be opened")

    def run_passes(self) -> list:
        results = []
        for _ in range(self.PASSES):
            results.extend(self_heal.run_once(self.root, self.failing_launch, create=self.create))
        return results

    def events(self, kind: str) -> list:
        return [event for event in board.snapshot(self.root)["events"] if event["kind"] == kind]

    def test_a_failing_reviewer_launch_is_capped_then_given_up_once(self):
        with board.locked_state(self.root) as state:
            state["reviewer_needed"] = {"requested_at": _ago(6 * 60), "request_id": "review-1"}
        self.run_passes()
        self.assertEqual(len(self.created), self_heal.MAX_ACTIONS_PER_HOUR, "at most 3 Reviewer sessions, not one per pass")
        self.assertEqual(len(self.launched), self_heal.MAX_ACTIONS_PER_HOUR)
        self.assertEqual(len(self.events("self_heal_gave_up")), 1, "one plain give-up line")
        self.assertEqual(len(self.events("self_heal_failed")), self_heal.MAX_ACTIONS_PER_HOUR)
        self.assertIn("could not: Terminal could not be opened", self.events("self_heal_failed")[0]["message"])

    def test_a_failing_relaunch_after_a_frozen_restart_is_capped_then_given_up_once(self):
        session, agent, process = self.delivery_with_task()
        self.freeze(session["id"])
        self.assertEqual(self_heal.run_once(self.root, self.failing_launch, create=self.create)[0]["phase"], "stopping")
        process.wait(timeout=5)
        board.offline(self.root, agent["id"], "visible CLI terminal ended", transport_ended=True)
        self.run_passes()
        self.assertLessEqual(len(self.launched), self_heal.MAX_ACTIONS_PER_HOUR - 1,
                             "the stop was attempt 1; each failed relaunch counts; the cap holds")
        self.assertGreaterEqual(len(self.launched), 1)
        self.assertEqual(len(self.events("self_heal_gave_up")), 1)
        self.assertNotIn(agent["task"], board.snapshot(self.root).get("cancelled_tasks", {}), "still never cancelled")

    def test_a_failing_relaunch_of_an_ended_terminal_is_capped_then_given_up_once(self):
        session, agent, process = self.delivery_with_task()
        process.kill(); process.wait(timeout=5)
        board.offline(self.root, agent["id"], "visible CLI terminal ended", transport_ended=True)
        self.run_passes()
        self.assertLessEqual(len(self.launched), self_heal.MAX_ACTIONS_PER_HOUR)
        self.assertGreaterEqual(len(self.launched), 1)
        self.assertEqual(len(self.events("self_heal_gave_up")), 1)

    def test_the_cap_does_not_depend_on_the_trimmed_event_window(self):
        with board.locked_state(self.root) as state:
            state["reviewer_needed"] = {"requested_at": _ago(6 * 60), "request_id": "review-1"}
        self.run_passes()
        with board.locked_state(self.root) as state:
            state["events"] = []   # a busy board trims its hot window
        self.run_passes()
        self.assertEqual(len(self.created), self_heal.MAX_ACTIONS_PER_HOUR, "the ledger, not the event window, holds the cap")


class WorkerWiringTests(unittest.TestCase):
    def test_the_worker_watchdog_runs_the_self_restart_after_each_active_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            watchdog = project_worker.ProjectWatchdog(Path(tmp))
            calls = []
            watchdog.self_heal = lambda: calls.append("ran") or []
            watchdog.tick()
            self.assertEqual(calls, ["ran"])
            self.assertEqual(watchdog.last_report.get("self_heal"), [])


PROBE = r"""
<script>
(async () => {
  const section = () => document.querySelector('#harness-actions');
  for (let attempt = 0; attempt < 150 && !(section() && !section().hidden && section().textContent.includes('harness')); attempt++)
    await new Promise(r => setTimeout(r, 100));
  const node = section();
  const box = node ? node.getBoundingClientRect() : {width: 0, height: 0};
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    text: node ? node.textContent.trim() : '', visible: box.width > 0 && box.height > 0,
    gaveUp: Array.from(document.querySelectorAll('#harness-actions li.gave-up')).map(li => li.textContent.trim())})});
})();
</script>
"""


class RenderedHarnessActionsTests(_Fixture):
    def setUp(self):
        super().setUp()
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        from tests.environment_support import require_loopback
        require_loopback()
        project_memory.initialize(self.root, project_name="Self restart proof", description="Facts.")

    def test_mission_control_says_what_the_harness_did(self):
        from tests.test_branding_rendered import probe_proxy
        session, agent, _ = self.delivery_with_task()
        with board.locked_state(self.root) as state:
            board._event(state, "agent_restart_requested", state["agents"][agent["id"]], {
                "task": agent["task"], "message": "The harness is restarting the Delivery Agent: its terminal showed nothing for 16 minutes while messages waited"})
            board._event(state, "self_heal_gave_up", None, {
                "task": "", "message": "The harness restarted the Reviewer 3 times in the last hour and it is still stuck (x). Please open its terminal to see what it needs."})
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Self restart proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="self-restart-proof", chat_action_token="owner-token"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close); self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory(); self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=1000)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        reading = sink.get("value")
        self.assertIsNotNone(reading, "Chrome reported nothing")
        self.assertTrue(reading["visible"], json.dumps(reading))
        self.assertIn("What the harness did for you", reading["text"])
        self.assertIn("restarting the Delivery Agent: its terminal showed nothing for 16 minutes", reading["text"])
        self.assertEqual(len(reading["gaveUp"]), 1, "the give-up line is marked for the owner")
        self.assertIn("Please open its terminal", reading["gaveUp"][0])


if __name__ == "__main__":
    unittest.main()
