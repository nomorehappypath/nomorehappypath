# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome) for the coverage check: under "What will be tested" each requirement says plainly covered, a gap and why, or not mapped.

Run:  PYTHONPATH=. python3 -m unittest tests.test_coverage_check_rendered -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, browser_acceptance, contract_check
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
    out.cards = [...document.querySelectorAll('#owner-actions .owner-action, #owner-actions article')].map(node => node.innerText);
  } catch (error) { out.error = String(error); }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""

LABELS = [
    {"n": 1, "criterion": "Running the program prints hello", "plain": "The program says hello when you run it.", "label": "checked", "note": "", "coverage": "covered"},
    {"n": 2, "criterion": "The greeting is friendly", "plain": "The greeting reads as friendly.", "label": "checked", "note": "", "coverage": contract_check.GAP_NORMAL_ONLY},
    {"n": 3, "criterion": "It remembers the name", "plain": "It remembers your name next time.", "label": "not checked in advance", "note": "", "coverage": contract_check.GAP_NO_TEST},
    {"n": 4, "criterion": "It also says goodbye", "plain": "The program says goodbye when it ends.", "label": "checked", "note": ""},
]


class RenderedCoverageTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_the_owner_reads_one_plain_coverage_line_per_requirement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            agent = delivery_with_direction(root, "TASK-ACTIVE")
            agreed_requirements(root, agent["id"], "Final agreed requirements: a command prints a greeting and remembers the name.")
            with board.locked_state(root) as state:
                state["requirement_confirmations"]["TASK-ACTIVE"]["contract_check"] = {
                    "status": "ready", "reason": "", "labels": [dict(item) for item in LABELS], "answers": [], "checks": [], "results": {}, "compare": {},
                }
            result = run_page(self, root, PROBE, width=1200, height=1000, wait=120)
        self.assertTrue(result and "error" not in result, result)
        self.assertEqual(result["errors"], [])
        lines = result["active"]
        self.assertEqual(len(lines), 4, lines)
        self.assertTrue(lines[0].endswith("covered"), lines[0])
        self.assertIn("gap: only the normal case is tested, no failure or unusual case", lines[1])
        self.assertIn("gap: no test covers this yet", lines[2])
        self.assertTrue(lines[3].endswith("not mapped"), "an older submission shows not mapped")
        self.assertEqual(result["cards"], [], "nothing is asked of the owner")


    def test_a_submission_without_keys_then_corrected_is_shown_covered(self):
        from tests.test_contract_first_checks import TASK as FIRST_TASK, TEXT, _Fixture, write_json
        from tests.test_coverage_check import CHECK
        fixture = _Fixture()
        fixture.setUp()
        self.addCleanup(fixture._tmp.cleanup)
        agent = fixture.delivery()
        _session, reviewer = fixture.reviewer()
        board.record_requirement_proposal(fixture.root, agent["id"], TEXT)
        plain = {"criteria": [{"text": "Running the program prints hello", "plain": "The program says hello when you run it.", "checks": [CHECK]}], "questions": []}
        fixture.submit(reviewer, plain)
        fix = write_json(fixture.base, "fix.json", {"checks": [], "failure_paths": ["a bad input is refused"]})
        board.record_contract_check_correction(fixture.root, reviewer["id"], FIRST_TASK, 1, "closing a coverage gap", fix)
        labels = contract_check.labels(fixture.proposal()["contract_check"]["criteria"])
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            owner_agent = delivery_with_direction(root, "TASK-ACTIVE")
            agreed_requirements(root, owner_agent["id"], "Final agreed requirements: a command prints a greeting.")
            with board.locked_state(root) as state:
                state["requirement_confirmations"]["TASK-ACTIVE"]["contract_check"] = {
                    "status": "ready", "reason": "", "labels": labels, "answers": [], "checks": [], "results": {}, "compare": {},
                }
            result = run_page(self, root, PROBE, width=1200, height=1000, wait=120)
        self.assertTrue(result and "error" not in result, result)
        self.assertEqual(len(result["active"]), 1, result["active"])
        self.assertTrue(result["active"][0].endswith("covered"), result["active"])
        self.assertNotIn("not mapped", result["active"][0])


if __name__ == "__main__":
    unittest.main()
