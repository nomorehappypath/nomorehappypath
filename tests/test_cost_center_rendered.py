# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome) for the cost center: the task card shows one plain cost sentence, the project header shows the total, markup is not run.

Run:  PYTHONPATH=. python3 -m unittest tests.test_cost_center_rendered -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, browser_acceptance, cost_center
from tests.environment_support import require_loopback
from tests.requirements_support import agreed_requirements
from tests.test_cost_center import CLAUDE_ID, T0, claude_row
from tests.test_no_terminal_questions_rendered import run_page
from tests.test_requirements_contract import context, delivery_with_direction

PROBE = r"""
<script>
(async () => {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const out = {errors: []};
  window.addEventListener('error', event => out.errors.push(String(event.message)));
  try {
    for (let attempt = 0; attempt < 200; attempt++) {
      if (document.querySelector('[data-task-cost]') && document.querySelector('#project-cost').textContent) break;
      await wait(150);
    }
    const node = document.querySelector('[data-task-cost]');
    out.line = node ? node.textContent : null;
    out.project = document.querySelector('#project-cost').textContent;
    out.injected = document.querySelectorAll('[data-task-cost] img, #project-cost img').length;
    out.asked = document.querySelectorAll('[data-cq]').length;
  } catch (error) { out.error = String(error); }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""


class RenderedCostTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        cost_center.configure(None)

    def test_the_owner_reads_the_cost_of_a_running_task_and_the_project_and_markup_is_not_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            agent = delivery_with_direction(root, "TASK-ACTIVE")
            agreed_requirements(root, agent["id"], "Final agreed requirements: a command prints a greeting.")
            store = Path(temporary) / "claude-home"
            transcript = store / "projects" / "-synthetic" / f"{CLAUDE_ID}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(claude_row("msg_1", 1, (1000, 500, 2000, 300)), encoding="utf-8")
            cost_center.collect(board.board_dir(root), [
                {"managed": "m1", "provider": "claude", "id": CLAUDE_ID, "store": str(store), "role": "delivery",
                 "holder": {"task": "TASK-ACTIVE", "spawned_at": (T0).isoformat()}},
                {"managed": "m2", "provider": "gemini", "id": "gem-conversation-1", "store": "", "role": "reviewer",
                 "holder": {"task": "TASK-ACTIVE", "spawned_at": (T0).isoformat()}},
            ], now=T0)
            result = run_page(self, root, PROBE, width=1200, height=1000, wait=120)
        self.assertTrue(result and "error" not in result, result)
        self.assertEqual(result["errors"], [])
        self.assertIn("Cost so far: about $0.0080 API-equivalent · 3.8K tokens (builder $0.0080).", result["line"])
        self.assertIn("Gemini (reviewer): not measured yet", result["line"])
        self.assertIn("Project so far: about $0.0080 API-equivalent", result["project"])
        self.assertIn("Excludes Gemini", result["project"])
        self.assertEqual(result["injected"], 0)
        self.assertEqual(result["asked"], 0, "the owner is not asked anything")


if __name__ == "__main__":
    unittest.main()
