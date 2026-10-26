# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome, both platforms) of the one plain line of what a task has used (task 2026-10-25_review-cost-controls).

The owner reads the line on the live task card and is never asked anything. Machine inputs removed: a temporary board root only.

Run:  PYTHONPATH=. python3 -m unittest tests.test_review_cost_controls_rendered -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, browser_acceptance
from tests.environment_support import require_loopback
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
      const line = document.querySelector('.task [data-task-usage]');
      if (line && line.innerText.trim()) break;
      await wait(150);
    }
    const line = document.querySelector('.task [data-task-usage]');
    const box = line ? line.getBoundingClientRect() : {width: 0, height: 0};
    out.text = line ? line.innerText : '';
    out.visible = box.width > 0 && box.height > 0;
    out.fontSize = line ? getComputedStyle(line).fontSize : '';
    out.cards = [...document.querySelectorAll('#owner-actions .owner-action, #owner-actions article')].map(node => node.innerText);
  } catch (error) { out.error = String(error); }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""


class RenderedUsageLineTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_the_live_card_shows_one_plain_line_and_asks_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            agent = delivery_with_direction(root, "TASK-USAGE")
            board.status(root, agent["id"], "working on it")
            result = run_page(self, root, PROBE, width=1200, height=1000, wait=120)
        self.assertTrue(result and "error" not in result, result)
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["visible"], result)
        self.assertIn("Counted so far: 2 recorded Delivery and Reviewer steps and 0 minutes of review.", result["text"])
        self.assertIn("not a provider bill", result["text"])
        self.assertGreaterEqual(float(result["fontSize"].rstrip("px") or 0), 13.0, "readable at the size it ships")
        self.assertEqual(result["cards"], [], "nothing is asked of the owner")


if __name__ == "__main__":
    unittest.main()
