# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof: the banner for agent questions and the reworded early-look card (task 2026-10-23_no-terminal-questions).

Run:  PYTHONPATH=. python3 -m unittest tests.test_no_terminal_questions_rendered -v
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
  const card = name => document.getElementById('req-decision-' + name);
  for (let attempt = 0; attempt < 120; attempt++) {
    const banner = document.getElementById('agent-questions');
    if (card('TASK-READING') && card('TASK-QUESTIONS') && banner && !banner.hidden) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const banner = document.getElementById('agent-questions');
  const rows = [...banner.querySelectorAll('[data-agent-question]')].map(row => row.textContent.replace(/\s+/g, ' ').trim());
  const targetsExist = [...banner.querySelectorAll('[data-jump-question]')].every(button => document.querySelector(button.dataset.jumpQuestion));
  const reading = card('TASK-READING');
  const buttons = [...reading.querySelectorAll('button')].map(button => button.textContent.trim());
  const text = reading.textContent.replace(/\s+/g, ' ');
  reading.querySelector('[data-req-wait]').click();
  await new Promise(resolve => setTimeout(resolve, 300));
  const after = card('TASK-READING');
  const noteEl = after && after.querySelector('[data-waiting-note]');
  const waitNote = noteEl ? noteEl.textContent : '';
  const buttonsAfter = after ? [...after.querySelectorAll('button')].map(button => [button.textContent.trim(), button.disabled]) : [];
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({heading: banner.querySelector('h2').textContent, rows, targetsExist, buttons, text, waitNote, buttonsAfter,
                          stillThere: Boolean(card('TASK-READING')), pageText: document.body.innerText})});
})();
</script>
"""

TESTED_PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 120; attempt++) {
    if (document.querySelectorAll('[data-checked-in-advance]').length >= 2) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const blocks = [...document.querySelectorAll('[data-checked-in-advance]')].map(block => block.textContent.replace(/\s+/g, ' '));
  const inCard = Boolean(document.querySelector('.requirements-proposal [data-checked-in-advance]'));
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({blocks, inCard})});
})();
</script>
"""

RECHECK_PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 120; attempt++) {
    if (document.querySelector('[data-contract-check="rechecking"]')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const note = document.querySelector('[data-contract-check="rechecking"]');
  const box = note ? note.closest('.requirements-proposal') : null;
  const buttons = box ? [...box.querySelectorAll('button')].map(button => button.textContent.trim()) : [];
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: note ? note.textContent.replace(/\s+/g, ' ') : '', buttons, list: box ? box.querySelectorAll('[data-checked-in-advance] .checked-in-advance-list li').length : 0})});
})();
</script>
"""

ASK_PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 120; attempt++) {
    const banner = document.getElementById('agent-questions');
    if (banner && !banner.hidden && banner.querySelector('[data-aq-choice]')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const banner = document.getElementById('agent-questions');
  const shown = {heading: banner.querySelector('h2')?.textContent || '', text: banner.textContent.replace(/\s+/g, ' '),
                 options: [...banner.querySelectorAll('[data-aq-choice]')].map(button => button.textContent.trim()),
                 hasBox: Boolean(banner.querySelector('[data-aq-text]'))};
  banner.querySelectorAll('[data-aq-choice]')[1].click();
  for (let attempt = 0; attempt < 60; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 200));
    if (document.getElementById('agent-questions').hidden) break;
  }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({shown, closed: document.getElementById('agent-questions').hidden})});
})();
</script>
"""

EMPTY_PROBE = r"""
<script>
(async () => {
  await new Promise(resolve => setTimeout(resolve, 2500));
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({hidden: document.getElementById('agent-questions').hidden})});
})();
</script>
"""


def run_page(self, root, probe, width=1300, height=1500, wait=90):
    server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    self.addCleanup(server.server_close)
    self.addCleanup(server.shutdown)
    sink: dict = {}
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, probe))
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    self.addCleanup(proxy.server_close)
    self.addCleanup(proxy.shutdown)
    profile = tempfile.TemporaryDirectory()
    self.addCleanup(profile.cleanup)
    process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=width, height=height)
    try:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and "value" not in sink:
            time.sleep(0.1)
        return sink.get("value") or {}
    finally:
        process.close()


class RenderedQuestionBannerTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_banner_counts_each_ask_once_and_the_card_uses_the_owners_wording(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = context(base)
            session = control.create(root, "claude_reviewer")
            reviewer = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
            control.take_instructions(root, session["id"])
            questions = [{"text": "Which greeting should it print?", "options": ["hello", "hi there"], "recommended": 0, "affects": [1]},
                         {"text": "Should it be loud?", "options": ["no", "yes"], "recommended": 0, "affects": []}]
            criteria = [{"text": "Prints a greeting", "checks": [{"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE}}]}]
            agent_q = delivery_with_direction(root, "TASK-QUESTIONS")
            board.record_requirement_proposal(root, agent_q["id"], "Final agreed requirements: a program prints a greeting.")
            path = base / "s.json"
            path.write_text(json.dumps(plain_ready({"criteria": criteria, "questions": questions})), encoding="utf-8")
            board.record_contract_check_submission(root, reviewer["id"], "TASK-QUESTIONS", 1, str(path))
            agent_r = delivery_with_direction(root, "TASK-READING")
            board.record_requirement_proposal(root, agent_r["id"], "Final agreed requirements: a page shows a clock.")
            cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
            board.record_owner_action(root, cto["id"], "Which port should the preview use?", why="Two ports are free.",
                                      kind="decision", default="use 8000", default_minutes=15)
            result = run_page(self, root, PROBE)
            self.assertTrue(result, "Chrome reported nothing")
            self.assertEqual(result["heading"], "3 questions from your agents need your answer")
            self.assertEqual(len(result["rows"]), 3)
            joined = " | ".join(result["rows"])
            for piece in ("The CTO asks", "Which port should the preview use?", "the CTO will use 8000", "The reviewer asks", "2 questions about",
                          "Delivery asks", "Your Go ahead on the requirements"):
                self.assertIn(piece, joined)
            self.assertTrue(result["targetsExist"], "every Show me button points at a card on the page")
            self.assertEqual(result["buttons"], ["Copy", "Go ahead without waiting", "Wait for the review", "Modify…"])
            self.assertIn("The reviewer is reviewing the contract", result["text"])
            self.assertIn("Do you want to go ahead without waiting, or wait?", result["text"])
            # Wait shows a visible note inside the card (the owner saw nothing happen on 2026-10-28) and the button reads as chosen.
            self.assertIn("You chose to wait", result["waitNote"])
            self.assertIn("nothing else to do", result["waitNote"])
            self.assertIn(["Waiting for the review…", True], result["buttonsAfter"])
            self.assertNotIn("Wait for the review", [name for name, _ in result["buttonsAfter"]])
            self.assertTrue(result["stillThere"])
            self.assertEqual(board.snapshot(root)["requirement_proposals"]["TASK-READING"]["status"], "awaiting_owner", "Wait writes nothing")

    def test_what_will_be_tested_shows_on_the_proposal_and_on_the_confirmed_contract_with_the_boards_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = context(base)
            session = control.create(root, "claude_reviewer")
            reviewer = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
            control.take_instructions(root, session["id"])
            value = {"criteria": [{"text": "Prints a greeting", "checks": [{"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE}}]},
                                  {"text": "The greeting looks friendly", "why_not_checked": "a matter of taste"}], "questions": []}
            (base / "s.json").write_text(json.dumps(plain_ready(value)), encoding="utf-8")
            one = delivery_with_direction(root, "TASK-CONFIRMED")
            board.record_requirement_proposal(root, one["id"], "Final agreed requirements: a program prints a greeting.")
            board.record_contract_check_submission(root, reviewer["id"], "TASK-CONFIRMED", 1, str(base / "s.json"))
            proposal = board.snapshot(root)["requirement_proposals"]["TASK-CONFIRMED"]
            board.record_requirements_decision(root, "TASK-CONFIRMED", "go_ahead", version=1, token=proposal["contract_check"]["token"])
            board.record_requirement_confirmation(root, one["id"], proposal["text"])
            board._record_contract_check_results(root, "TASK-CONFIRMED", {1: "passed"})
            two = delivery_with_direction(root, "TASK-READY")
            board.record_requirement_proposal(root, two["id"], "Final agreed requirements: a page shows a clock.")
            board.record_contract_check_submission(root, reviewer["id"], "TASK-READY", 1, str(base / "s.json"))
            result = run_page(self, root, TESTED_PROBE, wait=60)
            self.assertTrue(result, "Chrome reported nothing")
            self.assertTrue(result["inCard"], "the list is on the proposal card before the owner decides")
            joined = " | ".join(result["blocks"])
            self.assertIn("What will be tested", joined)
            self.assertIn("Written by the reviewer before coding; the board runs them", joined)
            self.assertIn("This requirement protects what you asked for. — checked · passed when the board ran it", joined)
            self.assertIn("This requirement protects what you asked for. — not checked in advance", joined)
            self.assertNotIn(".json", joined)
            self.assertNotIn("/", joined.replace("a matter of taste", ""))

    def test_after_the_owners_answers_the_card_says_the_reviewer_is_updating_the_tests(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = context(base)
            session = control.create(root, "claude_reviewer")
            reviewer = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
            control.take_instructions(root, session["id"])
            check = {"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE}}
            value = {"criteria": [{"text": "Prints a greeting", "checks": [check]}, {"text": "Exits cleanly", "checks": [check]}],
                     "questions": [{"text": "Which greeting should it print?", "options": ["hello", "hi there"], "recommended": 0, "affects": [1]}]}
            (base / "s.json").write_text(json.dumps(plain_ready(value)), encoding="utf-8")
            agent = delivery_with_direction(root, "TASK-RECHECK")
            board.record_requirement_proposal(root, agent["id"], "Final agreed requirements: a program prints a greeting.")
            board.record_contract_check_submission(root, reviewer["id"], "TASK-RECHECK", 1, str(base / "s.json"))
            token = board.snapshot(root)["requirement_proposals"]["TASK-RECHECK"]["contract_check"]["token"]
            board.record_requirements_decision(root, "TASK-RECHECK", "answer", version=1, token=token, answers=[{"question": 1, "choice": 1, "other": ""}])
            result = run_page(self, root, RECHECK_PROBE, wait=60)
            self.assertTrue(result, "Chrome reported nothing")
            self.assertIn("The reviewer is reviewing the contract based on your answers", result["text"])
            self.assertIn("Do you want to go ahead without waiting, or wait?", result["text"])
            self.assertEqual(result["buttons"], ["Copy", "Go ahead without waiting", "Wait for the review", "Modify…"])
            self.assertEqual(result["list"], 2, "What will be tested is listed")

    def test_delivery_alone_asks_the_owner_in_mission_control_and_the_answer_comes_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            agent = delivery_with_direction(root, "TASK-ASK")          # no CTO, no proposal: only Delivery asking
            control.take_instructions(root, agent["session_id"])
            board.record_agent_question(root, agent["id"], "Should the forecast show Celsius or Fahrenheit?", ["Celsius", "Fahrenheit"])
            result = run_page(self, root, ASK_PROBE, wait=60)
            self.assertTrue(result, "Chrome reported nothing")
            shown = result["shown"]
            self.assertEqual(shown["heading"], "1 question from your agents needs your answer")
            self.assertIn("Delivery asks", shown["text"])
            self.assertIn("Should the forecast show Celsius or Fahrenheit?", shown["text"])
            self.assertEqual(shown["options"], ["Celsius", "Fahrenheit"])
            self.assertTrue(shown["hasBox"])
            self.assertTrue(result["closed"], "the card closes once answered")
            sent = [i for i in control.take_instructions(root, agent["session_id"]) if i.get("source") == "owner-answer"]
            self.assertEqual(len(sent), 1)
            self.assertIn("Fahrenheit", sent[0]["text"])

    def test_banner_is_hidden_when_no_agent_needs_an_answer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            delivery_with_direction(root, "TASK-QUIET")
            self.assertTrue(run_page(self, root, EMPTY_PROBE, wait=45).get("hidden"))


if __name__ == "__main__":
    unittest.main()
