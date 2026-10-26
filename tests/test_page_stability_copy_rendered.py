# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (task 2026-10-24_page-stability-copy): the project page stops jumping while it updates, and its texts can be copied.

A real page with a working reviewer whose status changes every second, a confirmed agreement, an agent question and a
question card. It is refreshed every two seconds by the page itself; the probe holds a selection, types a draft, scrolls to the
top, pauses and resumes the project, and presses the Copy buttons.
Run:  PYTHONPATH=. python3 -m unittest tests.test_page_stability_copy_rendered -v
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from harness import board, browser_acceptance, control
from tests.environment_support import require_loopback
from tests.requirements_support import agreed_requirements
from tests.test_contract_first_checks import TEST_FILE, ledger, plain_ready
from tests.test_no_terminal_questions_rendered import run_page
from tests.test_requirements_contract import context, delivery_with_direction

AGREEMENT = "Objective: a page shows the weather for five days.\nAcceptance: each day shows a high and a low.\n  Indented line with *stars* and `code`."

PROBE = r"""
<script>
(async () => {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const out = {};
  try {
  for (let attempt = 0; attempt < 150; attempt++) {
    if (document.querySelector('.requirements-confirmation:not(.requirements-proposal) .requirements-body') && document.querySelector('[data-aq-text]')
        && document.querySelector('.agent-row')) break;
    await wait(150);
  }
  // 1. a selection inside the agreed requirements survives several refreshes while the reviewer's row keeps changing
  const body = document.querySelector('.requirements-confirmation:not(.requirements-proposal) .requirements-body');
  const details = body.closest('details'); if (details) details.open = true;   // the agreement sits behind "Full requirements": the owner opens it to select it
  const staticNode = body.closest('.task-static');
  let staticMutations = 0;
  new MutationObserver(list => { staticMutations += list.length; }).observe(staticNode, {childList: true, subtree: true});
  const range = document.createRange(); range.selectNodeContents(body);
  const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
  const wanted = selection.toString();
  const rowsText = () => [...document.querySelectorAll('.agent-row')].map(row => row.textContent).join('|');
  const rowBefore = rowsText();
  await wait(7500);
  out.selectionKept = selection.toString() === wanted && wanted.length > 20;
  out.selectedText = wanted.slice(0, 40);
  out.staticMutations = staticMutations;
  out.rowChanged = rowsText() !== rowBefore; out.rowSample = rowsText().slice(0, 600);
  // 2. a half-typed answer keeps its element, its text and its focus
  selection.removeAllRanges();
  const box = document.querySelector('[data-aq-text]');
  box.__mine = true; box.focus(); box.value = 'half typed answer';
  await wait(5500);
  const again = document.querySelector('[data-aq-text]');
  out.draftSameElement = Boolean(again && again.__mine); out.draftValue = again ? again.value : null; out.draftFocused = document.activeElement === again;
  document.activeElement.blur();
  // 3. a focused control in the agents list: the top of the page stays the top
  const button = document.querySelector('.agent-row button');
  if (button) button.focus();
  window.scrollTo(0, 0);
  await wait(5500);
  out.scrollYWithFocus = Math.round(window.scrollY);
  out.pageIsTall = document.documentElement.scrollHeight > innerHeight + 200;
  // 3b. the re-focus that used to scroll: a focused summary that is replaced
  const holder = document.createElement('div'); holder.id = 'probe-holder';
  holder.innerHTML = '<details data-attempt-key="probe"><summary>probe</summary>x</details>';
  document.body.appendChild(holder);
  holder.querySelector('summary').focus({preventScroll: true});
  window.scrollTo(0, 0);
  keepAttempts(holder, () => { holder.innerHTML = '<details data-attempt-key="probe"><summary>probe</summary>x</details>'; });
  await wait(300);
  out.scrollYAfterRefocus = Math.round(window.scrollY);
  holder.remove();
  // 4. Copy: exact raw text on success, the same text selected when the clipboard is missing or refuses
  const copyButton = document.querySelector('.requirements-confirmation:not(.requirements-proposal) [data-copy-key]');
  out.copyButtonExists = Boolean(copyButton);
  if (!copyButton) throw new Error('no Copy button on the agreed requirements');
  window.__copied = null;
  Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => { window.__copied = text; }}, configurable: true});
  copyButton.click(); await wait(300);
  out.copied = window.__copied; out.copiedLabel = copyButton.textContent;
  await wait(2300);
  Object.defineProperty(navigator, 'clipboard', {value: {writeText: async () => { throw new Error('refused'); }}, configurable: true});
  copyButton.click(); await wait(300);
  let area = document.querySelector('.copy-fallback');
  out.refusedFallback = Boolean(area) && area.value;
  out.refusedSelected = Boolean(area) && area.selectionStart === 0 && area.selectionEnd === area.value.length;
  out.refusedLabel = copyButton.textContent;
  area && area.remove();
  Object.defineProperty(navigator, 'clipboard', {value: undefined, configurable: true});
  copyButton.click(); await wait(300);
  area = document.querySelector('.copy-fallback');
  out.missingFallback = Boolean(area) && area.value;
  const directiveCopy = document.querySelector('.directive [data-copy-key]');
  out.directiveCopy = Boolean(directiveCopy);
  out.proposalCopy = Boolean(document.querySelector('.requirements-proposal [data-copy-key]'));
  } catch (error) { out.error = String(error); }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""

PAUSE_PROBE = r"""
<script>
(async () => {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const out = {};
  for (let attempt = 0; attempt < 150; attempt++) { if (document.querySelector('.task-dynamic .stop.cancel-task')) break; await wait(150); }
  const cancel = () => document.querySelector('.task-dynamic .stop.cancel-task');
  const copy = () => document.querySelector('[data-copy-key]');
  out.enabledBefore = !cancel().disabled;
  for (let attempt = 0; attempt < 120; attempt++) { if (document.body.classList.contains('project-paused')) break; await wait(250); }
  await wait(2500);
  out.pausedCancelDisabled = cancel().disabled; out.pausedCopyEnabled = Boolean(copy()) && !copy().disabled;
  for (let attempt = 0; attempt < 120; attempt++) { if (!document.body.classList.contains('project-paused')) break; await wait(250); }
  await wait(2500);
  out.resumedCancelEnabled = !cancel().disabled;
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""


class RenderedPageStabilityTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def fixture(self, base: Path):
        root = context(base)
        session = control.create(root, "claude_reviewer")
        reviewer = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        control.take_instructions(root, session["id"])
        a = delivery_with_direction(root, "TASK-CONFIRMED")
        agreed_requirements(root, a["id"], AGREEMENT)
        check = {"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE}}
        value = {"criteria": [{"text": "Prints a greeting", "checks": [check]}],
                 "questions": [{"text": "Which greeting should it print?", "options": ["hello", "hi there"], "recommended": 0, "affects": [1]}]}
        (base / "s.json").write_text(json.dumps(plain_ready(value)), encoding="utf-8")
        b = delivery_with_direction(root, "TASK-QUESTIONS")
        board.record_requirement_proposal(root, b["id"], "Final agreed requirements: another thing.")
        board.record_contract_check_submission(root, reviewer["id"], "TASK-QUESTIONS", 1, str(base / "s.json"))
        board.record_agent_question(root, a["id"], "How many days should the forecast cover?")
        return root, reviewer, a

    def test_selection_draft_scroll_and_copy_survive_the_two_second_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, reviewer, _a = self.fixture(Path(temporary))
            stop = threading.Event()

            def keep_working():
                number = 0
                while not stop.wait(0.7):
                    number += 1
                    try:
                        with board.locked_state(root) as state:   # the row shows the name: it changes every time
                            state["agents"][reviewer["id"]]["display_name"] = f"Reviewer working, step {number}"
                    except Exception:  # noqa: BLE001
                        pass
            worker = threading.Thread(target=keep_working, daemon=True)
            worker.start()
            self.addCleanup(worker.join, 3)
            self.addCleanup(stop.set)
            result = run_page(self, root, PROBE, width=1100, height=600, wait=120)
        self.assertTrue(result, "Chrome reported nothing")
        self.assertTrue(result["rowChanged"], "the reviewer's row did change during the test")
        self.assertTrue(result["selectionKept"], f"selection lost: {result}")
        self.assertEqual(result["staticMutations"], 0, "the agreed requirements were not touched by the refreshes")
        self.assertTrue(result["draftSameElement"] and result["draftFocused"], result)
        self.assertEqual(result["draftValue"], "half typed answer")
        self.assertTrue(result["pageIsTall"])
        self.assertEqual(result["scrollYWithFocus"], 0, "the page stayed at the top while the reviewer's row changed")
        self.assertEqual(result["scrollYAfterRefocus"], 0, "re-focusing a replaced summary must not scroll the page")
        self.assertTrue(result["copyButtonExists"] and result["directiveCopy"] and result["proposalCopy"])
        expected = AGREEMENT
        self.assertEqual(result["copied"], expected, "the exact stored text, not the formatted display")
        self.assertEqual(result["copiedLabel"], "Copied")
        self.assertEqual(result["refusedFallback"], expected)
        self.assertTrue(result["refusedSelected"])
        self.assertTrue(result["refusedLabel"].startswith("The text is selected"), result["refusedLabel"])
        self.assertEqual(result["missingFallback"], expected)

    def test_pause_and_resume_still_disable_and_recover_the_controls_and_copy_stays_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            agent = delivery_with_direction(root, "TASK-PAUSE")
            agreed_requirements(root, agent["id"], AGREEMENT)

            def pause_then_resume():
                time.sleep(6)
                board.begin_project_pause(root, drain_seconds=0)
                board.finish_project_pause(root)
                time.sleep(6)
                transaction = board.begin_project_resume(root)
                board.finish_project_resume(root, transaction["resume_id"])
            worker = threading.Thread(target=pause_then_resume, daemon=True)
            worker.start()
            self.addCleanup(worker.join, 5)
            result = run_page(self, root, PAUSE_PROBE, width=1100, height=900, wait=120)
        self.assertTrue(result, "Chrome reported nothing")
        self.assertTrue(result["enabledBefore"])
        self.assertTrue(result["pausedCancelDisabled"], "controls are disabled while paused")
        self.assertTrue(result["pausedCopyEnabled"], "copying is read-only and stays available")
        self.assertTrue(result["resumedCancelEnabled"], "controls recover after Resume")


if __name__ == "__main__":
    unittest.main()
