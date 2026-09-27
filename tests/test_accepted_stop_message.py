# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner report 2026-09-27: after accepting a final task, stopping its Delivery
said "Unfinished Delivery work was cleaned from the board". It must say
"Task accepted, Dev agent stopped." and must never cancel the accepted task.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance, contract, control, project_memory
from tests.requirements_support import agreed_requirements
from tests import test_board_viewer as _viewer_tests


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def delivery(self, task: str) -> dict:
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], f"OWNER DIRECTION — {task}")
        board.begin_task(self.root, agent["id"], task)
        agreed_requirements(self.root, agent["id"], f"Final agreed requirements for {task}: deliver and verify it.")
        contract.create_contract(self.root, task, f"OWNER DIRECTION — {task}", ["delivery"])
        return agent

    def accept(self, task: str, release_status: str = "VISUAL_TEST_REQUIRED") -> None:
        with board.locked_state(self.root) as state:
            state.setdefault("releases", {})[task] = {"status": release_status, "task": task, "cto_id": "cto-fixture", "recorded_at": board.now()}
            state.setdefault("release_decisions", {})[task] = {
                "task": task, "decision": "accepted", "recorded_at": board.now(),
            }


class AcceptedStopTests(_Fixture):
    def test_stopping_the_delivery_of_an_accepted_task_says_so_and_cancels_nothing(self):
        dev = self.delivery("ACCEPTED-TASK")
        self.accept("ACCEPTED-TASK")
        result = board.cancel_session_work(self.root, dev["session_id"])
        state = board.snapshot(self.root)
        self.assertEqual(result["accepted_tasks"], ["ACCEPTED-TASK"])
        self.assertEqual(result["cancelled_tasks"], [])
        # a finished, inactive Delivery of an accepted task is archived at once
        # (existing behaviour); the live list no longer holds it
        self.assertNotIn(dev["id"], state["agents"])
        self.assertEqual(state["release_decisions"]["ACCEPTED-TASK"]["decision"], "accepted")
        self.assertNotIn("ACCEPTED-TASK", state.get("cancelled_tasks", {}))
        kinds = [event["kind"] for event in state["events"]]
        self.assertNotIn("task_cancelled", kinds)
        stopped = [event for event in state["events"] if event["kind"] == "delivery_stopped_after_acceptance"]
        self.assertEqual([(event["agent_id"], event["message"]) for event in stopped],
                         [(dev["id"], "Task accepted, Dev agent stopped.")])

    def test_the_acceptance_itself_guards_the_task_whatever_its_release_status_reads(self):
        dev = self.delivery("ACCEPTED-LATER-STATUS")
        self.accept("ACCEPTED-LATER-STATUS", release_status="ACCEPTED")
        result = board.cancel_session_work(self.root, dev["session_id"])
        self.assertEqual(result["cancelled_tasks"], [], "an accepted task is never cancelled by Stop")
        self.assertEqual(result["accepted_tasks"], ["ACCEPTED-LATER-STATUS"])
        state = board.snapshot(self.root)
        self.assertEqual(state["release_decisions"]["ACCEPTED-LATER-STATUS"]["decision"], "accepted")
        self.assertEqual(state["releases"]["ACCEPTED-LATER-STATUS"]["status"], "ACCEPTED")
        self.assertNotIn("ACCEPTED-LATER-STATUS", state.get("cancelled_tasks", {}))
        self.assertNotIn("task_cancelled", [event["kind"] for event in state["events"]])

    def test_an_unfinished_task_is_still_cancelled_as_before(self):
        dev = self.delivery("UNFINISHED-TASK")
        result = board.cancel_session_work(self.root, dev["session_id"])
        state = board.snapshot(self.root)
        self.assertEqual(result["cancelled_tasks"], ["UNFINISHED-TASK"])
        self.assertEqual(result["accepted_tasks"], [])
        self.assertIn("task_cancelled", [event["kind"] for event in state["events"]])


class AcceptedStopPageTests(unittest.TestCase):
    """The page function, executed (node), with the viewer suite's own runner
    (borrowed, not inherited, so the viewer suite is not run a second time)."""

    script = _viewer_tests.BoardViewerTests.script
    declarations_only = _viewer_tests.BoardViewerTests.declarations_only
    run_node = _viewer_tests.BoardViewerTests.run_node

    def _run(self, decision: str | None) -> dict:
        state = {"release_decisions": {"TASK-ONE": {"decision": decision}}} if decision else {}
        return self.run_node("""
const nodes={notice:{textContent:''}};
globalThis.document={querySelector(selector){return nodes[selector.slice(1)]||null;}};
let prompt='';
globalThis.window={confirm(message){prompt=message;return true;}};
globalThis.fetch=async path=>({ok:true,json:async()=>({cleanup:{accepted_tasks:[],cancelled_tasks:[]}})});
refresh=async()=>{};
lastBoard={state:%s};
(async()=>{
  await confirmStopSession('codex-one','CODEX CLI · Delivery Agent','Task One','COMPLETE','TASK-ONE');
  process.stdout.write(JSON.stringify({prompt,notice:nodes.notice.textContent}));
})();
""" % json.dumps(state))

    def test_an_accepted_task_gets_the_accepted_prompt_and_notice(self):
        result = self._run("accepted")
        self.assertEqual(result["notice"], "Task accepted, Dev agent stopped.")
        self.assertIn("accepted and already in main", result["prompt"])
        self.assertIn("nothing is removed", result["prompt"])
        self.assertNotIn("will be removed", result["prompt"])

    def test_an_unfinished_task_keeps_the_unfinished_wording(self):
        result = self._run(None)
        self.assertIn("Unfinished Delivery work was cleaned from the board.", result["notice"])
        self.assertIn("will be removed", result["prompt"])


PROBE = r"""
<script>
(async () => {
  let prompt = '';
  window.confirm = message => { prompt = message; return true; };
  const findStop = () => Array.from(document.querySelectorAll('#agents .agent-row button, #sessions button'))
    .find(button => button.textContent.trim() === 'Stop terminal');
  for (let attempt = 0; attempt < 150 && !findStop(); attempt++) await new Promise(r => setTimeout(r, 100));
  const stop = findStop();
  if (stop) stop.click();
  const notice = () => (document.querySelector('#notice')?.textContent || '').trim();
  for (let attempt = 0; attempt < 100 && !/stopped|cleaned/i.test(notice()); attempt++) await new Promise(r => setTimeout(r, 100));
  const node = document.querySelector('#notice');
  const box = node ? node.getBoundingClientRect() : {width: 0, height: 0};
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({found: Boolean(stop), prompt, notice: notice(), visible: box.width > 0 && box.height > 0})});
})();
</script>
"""


class RenderedAcceptedStopTests(_Fixture):
    def setUp(self):
        super().setUp()
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        from tests.environment_support import require_loopback
        require_loopback()
        project_memory.initialize(self.root, project_name="Accepted stop proof", description="Facts.")

    def test_the_real_page_says_task_accepted_dev_agent_stopped(self):
        from tests.test_branding_rendered import probe_proxy
        dev = self.delivery("ACCEPTED-TASK")
        self.accept("ACCEPTED-TASK")
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Accepted stop proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="accepted-stop-proof",
            chat_action_token="owner-token",
        ))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=1000)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        reading = sink.get("value")
        self.assertIsNotNone(reading, "Chrome reported nothing")
        self.assertTrue(reading["found"], json.dumps(reading))
        self.assertIn("accepted and already in main", reading["prompt"])
        self.assertEqual(reading["notice"], "Task accepted, Dev agent stopped.", json.dumps(reading))
        self.assertTrue(reading["visible"], "the notice has no geometry")
        state = board.snapshot(self.root)
        self.assertNotIn("ACCEPTED-TASK", state.get("cancelled_tasks", {}))
        self.assertEqual(state["release_decisions"]["ACCEPTED-TASK"]["decision"], "accepted")


if __name__ == "__main__":
    unittest.main()
