# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome): the owner reads one plain, escaped line per builder deviation of a locked check (task 2026-10-26_check-deviation).

Run:  PYTHONPATH=. python3 -m unittest tests.test_check_deviation_rendered -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, browser_acceptance
from tests.environment_support import require_loopback
from tests.requirements_support import agreed_requirements
from tests.test_no_terminal_questions_rendered import run_page
from tests.test_requirements_contract import context, delivery_with_direction

PROBE = r"""
<script>
(async () => {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const out = {errors: []};
  window.addEventListener('error', event => out.errors.push(String(event.message)));
  try {
    const history = document.querySelector('#history');
    if (history && !history.open) history.open = true;
    for (let attempt = 0; attempt < 200; attempt++) {
      document.querySelectorAll('details.history-date-group').forEach(group => { group.open = true; });
      if (document.querySelector('.task [data-checked-in-advance]')) break;
      await wait(150);
    }
    const lines = selector => [...document.querySelectorAll(selector + ' [data-checked-in-advance] .checked-in-advance-list li')].map(li => li.textContent.replace(/\s+/g, ' '));
    out.active = lines('.task .requirements-confirmation');
    out.deviations = [...document.querySelectorAll('.task [data-check-deviations] li')].map(li => li.textContent.replace(/\s+/g, ' '));
    out.injected = document.querySelectorAll('.task [data-check-deviations] b, .task [data-check-deviations] img').length;
    out.cards = [...document.querySelectorAll('#owner-actions .owner-action, #owner-actions article')].map(node => node.innerText);
  } catch (error) { out.error = String(error); }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""

LABELS = [{"n": 1, "criterion": "Running the program prints hello", "plain": "The program says hello when you run it.", "label": "checked", "note": "", "coverage": "covered"}]
DEVIATIONS = [
    {"id": "dev-1", "criterion_index": 1, "check": "1.1", "why": "it assumes a greeting field but the API only has text <b>bold</b>", "status": "replaced", "answer_reason": "fixed"},
    {"id": "dev-2", "criterion_index": 1, "check": "1.2", "why": "it assumes port 80 but the app uses PORT", "status": "kept", "answer_reason": "the spec names port 80 <img src=x>"},
    {"id": "dev-3", "criterion_index": 1, "check": "1.3", "why": "it assumes a login page", "status": "open"},
]


class RenderedCheckDeviationTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_the_owner_reads_one_plain_escaped_line_per_deviation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            agent = delivery_with_direction(root, "TASK-ACTIVE")
            agreed_requirements(root, agent["id"], "Final agreed requirements: a command prints a greeting and remembers the name.")
            with board.locked_state(root) as state:
                state["requirement_confirmations"]["TASK-ACTIVE"]["contract_check"] = {
                    "status": "ready", "reason": "", "labels": [dict(item) for item in LABELS], "answers": [], "checks": [], "results": {}, "compare": {},
                    "deviations": [dict(item) for item in DEVIATIONS],
                }
            result = run_page(self, root, PROBE, width=1200, height=1000, wait=120)
        self.assertTrue(result and "error" not in result, result)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["deviations"], [
            "The builder said check 1.1 did not match reality: it assumes a greeting field but the API only has text <b>bold</b>. The reviewer changed the check.",
            "The builder said check 1.2 did not match reality: it assumes port 80 but the app uses PORT. The reviewer kept it: the spec names port 80 <img src=x>.",
            "The builder said check 1.3 did not match reality: it assumes a login page. The reviewer has not answered yet.",
        ])
        self.assertEqual(result["injected"], 0, "the text is escaped, never markup")
        self.assertEqual(result["cards"], [], "nothing is asked of the owner")


if __name__ == "__main__":
    unittest.main()
