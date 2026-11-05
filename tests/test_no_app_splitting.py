# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The app never splits a task on its own (owner 2026-11-04): a chunked or application plan needs the owner's direction to allow it.

Run:  PYTHONPATH=. python3 -m unittest tests.test_no_app_splitting -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control
from tests.requirements_support import agreed_requirements


class NoAppSplittingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")

    def delivery(self, task: str, *, allow_split: bool, mission_control: bool):
        """A Delivery agent with an agreed task; the direction comes from Mission Control (the box) or the terminal."""
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        objective = f"OWNER DIRECTION — {task}"
        if mission_control:
            board.record_owner_message(self.root, agent["id"], objective, "direction", [], allow_split=allow_split)
        else:
            board.record_owner_direction(self.root, session["id"], objective, allow_split=allow_split)
        board.begin_task(self.root, agent["id"], task)
        contract.create_contract(self.root, task, objective, ["delivery"])
        agreed_requirements(self.root, agent["id"], f"Final agreed requirements for {task}: deliver the objective and verify every scenario.")
        return agent

    def test_a_direction_that_did_not_allow_a_split_gets_an_atomic_plan_only(self):
        agent = self.delivery("ONE-PIECE", allow_split=False, mission_control=True)
        self.assertFalse(board.snapshot(self.root)["task_split_allowed"]["ONE-PIECE"])
        for mode in ("chunked", "application"):
            with self.assertRaisesRegex(ValueError, "the owner did not ask for this task to be split"):
                board.define_delivery_plan(self.root, agent["id"], mode, "big job")
        self.assertNotIn("ONE-PIECE", board.snapshot(self.root)["delivery_plans"])
        plan = board.define_delivery_plan(self.root, agent["id"], "atomic", "one build, one review")
        self.assertEqual(plan["mode"], "atomic")

    def test_the_owner_box_allows_a_split(self):
        agent = self.delivery("SPLIT-OK", allow_split=True, mission_control=True)
        snapshot = board.snapshot(self.root)
        self.assertTrue(snapshot["task_split_allowed"]["SPLIT-OK"])
        self.assertTrue(snapshot["owner_directions"][agent["session_id"]]["allow_split"])
        self.assertEqual(board.define_delivery_plan(self.root, agent["id"], "chunked", "two independent parts")["mode"], "chunked")

    def test_a_terminal_direction_never_allows_a_split_by_itself(self):
        agent = self.delivery("TERMINAL", allow_split=False, mission_control=False)
        with self.assertRaisesRegex(ValueError, "did not ask"):
            board.define_delivery_plan(self.root, agent["id"], "chunked", "big job")
        allowed = self.delivery("TERMINAL-FLAG", allow_split=True, mission_control=False)
        self.assertEqual(board.define_delivery_plan(self.root, allowed["id"], "application", "a product")["mode"], "application")


class FormFieldTests(unittest.TestCase):
    """Round 1 (Codex R1-1): the form parser dropped allow_split, so a ticked box arrived as off."""

    def test_the_ticked_box_survives_the_multipart_form(self):
        from harness import board_viewer
        boundary = "----nmhp-split-test"
        parts = {"message_type": "direction", "text": "OWNER DIRECTION — build it in parts", "task_kind": "development", "allow_split": "1"}
        body = "".join(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n" for name, value in parts.items()) + f"--{boundary}--\r\n"
        fields, attachments = board_viewer.parse_release_multipart(body.encode("utf-8"), f"multipart/form-data; boundary={boundary}")
        self.assertEqual(attachments, [])
        self.assertEqual(fields["allow_split"], "1")
        self.assertEqual(fields["task_kind"], "development")

class RouteAndBrowserTests(unittest.TestCase):
    """The owner's box reaches the board through the real form route and the real dialog (round 1: the parser dropped it)."""
    from tests.test_long_directive_ui import LongDirectiveUITests as _Long
    setUp = _Long.setUp
    waiting_agent = _Long.waiting_agent
    served = _Long.served
    render = _Long.render
    multipart = staticmethod(_Long.multipart)
    post_owner_message = _Long.post_owner_message

    def test_the_form_route_stores_the_box_on_and_off(self):
        server, thread, origin = self.served()
        try:
            for value, expected in (("1", True), ("", False)):
                session, agent = self.waiting_agent()
                self.post_owner_message(origin, agent["id"], {"message_type": "direction", "text": f"OWNER DIRECTION — box {value or 'off'}", "task_kind": "development", "allow_split": value})
                self.assertIs(board.snapshot(self.root)["owner_directions"][session["id"]]["allow_split"], expected)
        finally:
            server.shutdown(); thread.join(timeout=3); server.server_close()

    def test_the_real_dialog_sends_the_ticked_box(self):
        session, agent = self.waiting_agent()
        server, thread, origin = self.served()
        script = f"""
<script>
(async()=>{{
  const pause=delay=>new Promise(resolve=>setTimeout(resolve,delay));
  for(let attempt=0;attempt<100;attempt++){{
    const button=Array.from(document.querySelectorAll('button')).find(item=>item.textContent.trim()==='Give direction');
    if(button){{button.click();break;}} await pause(100);
  }}
  const box=document.querySelector('#owner-message-allow-split'),label=document.querySelector('#owner-message-split');
  const shown=!!box&&!label.hidden,offByDefault=box&&!box.checked;
  box.checked=true;
  const textarea=document.querySelector('#owner-message-text');
  textarea.value='OWNER DIRECTION — build it in separately reviewed parts';textarea.dispatchEvent(new Event('input',{{bubbles:true}}));
  ownerMessageAgentId='{agent["id"]}';document.querySelector('#owner-message-form').requestSubmit();
  for(let attempt=0;attempt<100&&document.querySelector('#owner-message-dialog').open;attempt++)await pause(100);
  const dashboard=await (await fetch('/api/dashboard')).json(),saved=dashboard.state.owner_directions['{session["id"]}'];
  await fetch('/__layout_result__',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{shown,offByDefault,allowSplit:saved&&saved.allow_split===true}})}});
}})();
</script>
"""
        try:
            result = self.render(origin, script)
        finally:
            server.shutdown(); thread.join(timeout=3); server.server_close()
        self.assertTrue(result["shown"], "the box is in the Give direction dialog")
        self.assertTrue(result["offByDefault"], "off unless the owner ticks it")
        self.assertTrue(result["allowSplit"], "the ticked box is stored on the direction")
        self.assertTrue(board.snapshot(self.root)["owner_directions"][session["id"]]["allow_split"])

if __name__ == "__main__":
    unittest.main()
