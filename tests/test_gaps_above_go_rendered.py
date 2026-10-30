# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome): requirements with a test gap are summed up in plain words ABOVE the Go ahead buttons; Go ahead stays enabled.

Run:  PYTHONPATH=. python3 -m unittest tests.test_gaps_above_go_rendered -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness import board, browser_acceptance, control
from tests.environment_support import require_loopback
from tests.test_coverage_check import criteria
from tests.test_no_terminal_questions_rendered import run_page
from tests.test_requirements_contract import context, delivery_with_direction

PROBE = r"""
<script>
(async () => {
  const names = ['TASK-GAPS', 'TASK-COVERED'];
  const card = name => document.getElementById('req-decision-' + name);
  for (let attempt = 0; attempt < 150; attempt++) {
    if (names.every(name => card(name))) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const describe = name => {
    const box = card(name);
    if (!box) return null;
    const summary = box.querySelector('[data-gap-summary]');
    const go = box.querySelector('[data-req-go]');
    return {
      summary: summary ? summary.innerText.replace(/\s+/g, ' ').trim() : null,
      items: summary ? [...summary.querySelectorAll('li')].map(li => li.textContent) : [],
      above: Boolean(summary && go && (summary.compareDocumentPosition(go) & Node.DOCUMENT_POSITION_FOLLOWING)),
      goEnabled: Boolean(go) && !go.disabled && go.getBoundingClientRect().width > 0,
      buttons: [...box.querySelectorAll('button')].map(button => button.textContent.trim()),
      asked: box.querySelectorAll('[data-cq]').length,
    };
  };
  const out = Object.fromEntries(names.map(name => [name, describe(name)]));
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""


class RenderedGapsAboveGoTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_gaps_are_summed_up_above_go_ahead_and_nothing_shows_when_covered(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = context(base)
            session = control.create(root, "claude_reviewer")
            reviewer = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
            control.take_instructions(root, session["id"])
            from tests.test_contract_first_checks import plain_ready
            # requirement 1 covered, 2 normal case only, 3 not tested at all
            for task, specs in (("TASK-GAPS", [(True, True), (True, False), (False, False)]), ("TASK-COVERED", [(True, True)])):
                agent = delivery_with_direction(root, task)
                board.record_requirement_proposal(root, agent["id"], f"Final agreed requirements for {task}.")
                path = base / f"{task}.json"
                path.write_text(json.dumps(plain_ready(criteria(*specs))), encoding="utf-8")
                board.record_contract_check_submission(root, reviewer["id"], task, 1, str(path))
            result = run_page(self, root, PROBE, width=1200, height=1600, wait=120)
        self.assertTrue(result, "Chrome reported nothing")
        gaps = result["TASK-GAPS"]
        self.assertIn("Before you go ahead: 2 requirements are not fully tested yet", gaps["summary"])
        self.assertIn("The reviewer has been asked to add them; you can go ahead now or wait.", gaps["summary"])
        self.assertEqual(len(gaps["items"]), 2, gaps["items"])
        self.assertTrue(gaps["items"][0].startswith("2. "), gaps["items"])
        self.assertIn("only the normal case is tested", gaps["items"][0])
        self.assertTrue(gaps["items"][1].startswith("3. "), gaps["items"])
        self.assertTrue(gaps["above"], "the summary sits above the Go ahead button")
        self.assertTrue(gaps["goEnabled"], "Go ahead is never held")
        self.assertEqual(gaps["asked"], 0, "no new question for the owner")
        covered = result["TASK-COVERED"]
        self.assertIsNone(covered["summary"], "nothing is shown when everything is covered")
        self.assertTrue(covered["goEnabled"])


if __name__ == "__main__":
    unittest.main()
