# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof: the owner sees the held Go ahead, the one question card and the "not checked in advance" fallback.

One real page, three tasks: A (the reviewer is reading), B (the reviewer has four questions), C (no reviewer was free).
Run:  PYTHONPATH=. python3 -m unittest tests.test_contract_first_checks_rendered -v
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance, control
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_contract_first_checks import ledger, TEST_FILE, plain_ready
from tests.test_requirements_contract import context, delivery_with_direction

PROBE = r"""
<script>
(async () => {
  const names = ['TASK-READING', 'TASK-QUESTIONS', 'TASK-FALLBACK'];
  const card = name => document.getElementById('req-decision-' + name);
  for (let attempt = 0; attempt < 120; attempt++) {
    if (names.every(name => card(name))) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const describe = name => {
    const box = card(name);
    if (!box) return null;
    const visible = element => Boolean(element) && element.getBoundingClientRect().width > 0;
    return {
      text: box.textContent,
      buttons: [...box.querySelectorAll('button')].map(button => ({text: button.textContent.trim(), visible: visible(button)})),
      questions: [...box.querySelectorAll('[data-cq]')].map(field => ({
        legend: field.querySelector('legend').textContent,
        options: [...field.querySelectorAll('label.cq-option')].map(label => label.textContent.replace(/\s+/g, ' ').trim()),
        checkedFirst: field.querySelector('input[type=radio]').checked,
        hasOther: Boolean(field.querySelector('[data-cq-other]')),
      })),
    };
  };
  const before = Object.fromEntries(names.map(name => [name, describe(name)]));
  // the owner types his own answer to question 2 and answers the card
  window.confirm = () => true;
  const box = card('TASK-QUESTIONS');
  const second = box.querySelectorAll('[data-cq]')[1];
  second.querySelector('input[value="other"]').click();
  second.querySelector('[data-cq-other]').value = 'a loud HELLO';
  box.querySelector('[data-cq-answer]').click();
  let note = '';
  for (let attempt = 0; attempt < 60; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 200));
    note = document.getElementById('req-decision-note-TASK-QUESTIONS')?.textContent || '';
    if (/Could not/.test(note) || card('TASK-QUESTIONS')?.querySelector('[data-contract-check="ready"]')) break;   // the card turns into the "answers recorded" state
  }
  const after = document.getElementById('tasks').textContent;
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({before, note, after})});
})();
</script>
"""


class RenderedContractCheckTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_held_go_ahead_question_card_and_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = context(base)
            session = control.create(root, "claude_reviewer")
            reviewer = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
            control.take_instructions(root, session["id"])
            questions = [
                {"text": "Which greeting should it print?", "options": ["hello", "hi there", "good day"], "recommended": 1, "affects": [1]},
                {"text": "Should the greeting be loud?", "options": ["no", "yes"], "recommended": 0, "affects": []},
            ]
            criteria = [{"text": "Running the program prints a greeting", "checks": [{"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE}}]}]
            # B first (the reviewer submits, then is free again), then A (reading), then C (the only reviewer is busy reading A)
            agent_b = delivery_with_direction(root, "TASK-QUESTIONS")
            board.record_requirement_proposal(root, agent_b["id"], "Final agreed requirements: a program prints a greeting.")
            path = base / "submission.json"
            path.write_text(json.dumps(plain_ready({"criteria": criteria, "questions": questions})), encoding="utf-8")
            board.record_contract_check_submission(root, reviewer["id"], "TASK-QUESTIONS", 1, str(path))
            agent_a = delivery_with_direction(root, "TASK-READING")
            board.record_requirement_proposal(root, agent_a["id"], "Final agreed requirements: a page shows a clock.")
            # the third Delivery is the same vendor as the reviewer, so it is not eligible (the machine allows only two Codex terminals)
            session_c = control.create(root, "claude_cto")
            agent_c = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="Anthropic", session_id=session_c["id"])
            board.record_owner_direction(root, session_c["id"], "OWNER DIRECTION — TASK-FALLBACK: write a report.")
            board.begin_task(root, agent_c["id"], "TASK-FALLBACK")
            board.record_requirement_proposal(root, agent_c["id"], "Final agreed requirements: a report is written.")
            states = {task: board.snapshot(root)["requirement_proposals"][task]["contract_check"]["status"]
                      for task in ("TASK-READING", "TASK-QUESTIONS", "TASK-FALLBACK")}
            self.assertEqual(states, {"TASK-READING": "reading", "TASK-QUESTIONS": "questions", "TASK-FALLBACK": "not_checked"})

            server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
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
            process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1300, height=1500)
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and "value" not in sink:
                    time.sleep(0.1)
                result = sink.get("value") or {}
            finally:
                process.close()
            self.assertTrue(result, "Chrome reported nothing")
            before = result["before"]

            reading = before["TASK-READING"]
            self.assertIn("The reviewer is reviewing the contract", reading["text"])
            self.assertEqual([b["text"] for b in reading["buttons"]], ["Copy", "Go ahead without waiting", "Wait for the review", "Modify…"])
            self.assertTrue(all(b["visible"] for b in reading["buttons"]), "the Go ahead button is not hidden or held")

            card = before["TASK-QUESTIONS"]
            self.assertEqual([b["text"] for b in card["buttons"]], ["Copy", "Answer the questions", "Go ahead using the recommended answers", "Modify…"])
            self.assertTrue(all(b["visible"] for b in card["buttons"]))
            self.assertLessEqual(len(card["questions"]), 4)
            first = card["questions"][0]
            self.assertTrue(first["options"][0].startswith("hi there") and "(recommended)" in first["options"][0], "the recommended answer comes first")
            self.assertTrue(first["checkedFirst"], "the recommended answer is already chosen")
            self.assertTrue(first["options"][-1].startswith("Other"))
            self.assertTrue(all(q["hasOther"] for q in card["questions"]))
            self.assertIn("hi there", card["text"])

            fallback = before["TASK-FALLBACK"]
            self.assertIn("Not checked in advance", fallback["text"])
            self.assertEqual([b["text"] for b in fallback["buttons"]][:2], ["Copy", "Go ahead — this is the contract"])

            self.assertNotIn("Could not", result["note"])
            self.assertIn("Your answers are recorded under", result["after"])
            proposal = board.snapshot(root)["requirement_proposals"]["TASK-QUESTIONS"]
            self.assertEqual((proposal["status"], proposal["version"]), ("awaiting_owner", 2), "answering is not accepting")
            self.assertIn("Which greeting should it print? -> hi there (recommended)", proposal["text"])
            self.assertIn("Should the greeting be loud? -> a loud HELLO", proposal["text"])
            self.assertFalse(proposal["contract_check"]["used_recommended"])


if __name__ == "__main__":
    unittest.main()
