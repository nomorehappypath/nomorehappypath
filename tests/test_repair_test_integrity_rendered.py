# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome, both platforms) for task 2026-10-25_repair-test-integrity.

The owner reads "What will be tested" in two places, the current requirements and the history of a finished task, and both must say in plain
words whether each check was seen failing before the work. Nothing is asked of him. Machine inputs removed: nothing is read from the machine;
the project is a temporary folder and the stored labels are written into its board state.

Run:  PYTHONPATH=. python3 -m unittest tests.test_repair_test_integrity_rendered -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, browser_acceptance, control, red_proof
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
      if (document.querySelector('.task [data-checked-in-advance]') && document.querySelector('.history-item [data-checked-in-advance]')) break;
      await wait(150);
    }
    const lines = selector => [...document.querySelectorAll(selector + ' [data-checked-in-advance] .checked-in-advance-list li')].map(li => li.textContent.replace(/\s+/g, ' '));
    out.active = lines('.task .requirements-confirmation');
    out.history = lines('.history-item');
    out.cards = [...document.querySelectorAll('#owner-actions .owner-action, #owner-actions article')].map(node => node.innerText);
  } catch (error) { out.error = String(error); }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""

LABELS = [
    {"n": 1, "criterion": "Running the program prints hello", "plain": "The program says hello when you run it.", "label": "checked", "note": ""},
    {"n": 2, "criterion": "The greeting is friendly", "plain": "The greeting reads as friendly.", "label": "checked", "note": ""},
    {"n": 3, "criterion": "It remembers the name", "plain": "It remembers your name next time.", "label": "checked", "note": ""},
]


class RenderedRedProofTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_the_current_view_and_the_history_say_the_same_plain_things(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            session = control.create(root, "claude_cto")
            cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
            for task in ("TASK-ACTIVE", "TASK-DONE"):
                agent = delivery_with_direction(root, task)
                agreed_requirements(root, agent["id"], "Final agreed requirements: a command prints a greeting and remembers the name.")
            with board.locked_state(root) as state:
                for task in ("TASK-ACTIVE", "TASK-DONE"):
                    state["requirement_confirmations"][task]["contract_check"] = {
                        "status": "ready", "reason": "", "labels": [dict(item) for item in LABELS], "answers": [], "checks": [],
                        "results": {"1": "passed", "2": "passed"},
                        "compare": {"1": red_proof.FAILS_BEFORE, "2": red_proof.PASSES_ON_NOTHING, "3": red_proof.COULD_NOT_COMPARE},
                    }
                state["releases"]["TASK-DONE"] = {"task": "TASK-DONE", "status": "RELEASE_ACCEPTED", "cto_id": cto["id"], "recorded_at": board.now(), "head_commit": "c0ffee"}
                state["release_decisions"]["TASK-DONE"] = {"task": "TASK-DONE", "decision": "accepted", "recorded_at": board.now()}
            result = run_page(self, root, PROBE, width=1200, height=1000, wait=120)
        self.assertTrue(result and "error" not in result, result)
        self.assertEqual(result["errors"], [])
        for view in ("active", "history"):
            lines = result[view]
            self.assertEqual(len(lines), 3, (view, lines))
            self.assertIn("fails before the work (good)", lines[0], (view, lines))
            self.assertIn("passes on nothing: did not distinguish", lines[1], (view, lines))
            self.assertNotIn("passed when the board ran it", lines[1], "a check that proved nothing is never shown as passed")
            self.assertIn("could not compare with the project before the work (no failure of the work)", lines[2], (view, lines))
        self.assertEqual(result["cards"], [], "nothing is asked of the owner")


if __name__ == "__main__":
    unittest.main()
