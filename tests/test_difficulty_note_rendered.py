# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome) for the difficulty note: the task card shows one plain line per role, and the text is escaped.

Run:  PYTHONPATH=. python3 -m unittest tests.test_difficulty_note_rendered -v
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
    for (let attempt = 0; attempt < 200; attempt++) {
      if (document.querySelector('[data-task-difficulty]')) break;
      await wait(150);
    }
    const node = document.querySelector('[data-task-difficulty]');
    out.line = node ? node.textContent : null;
    out.injected = document.querySelectorAll('[data-task-difficulty] img').length;
  } catch (error) { out.error = String(error); }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""


class RenderedDifficultyNoteTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_the_owner_reads_one_plain_line_and_markup_is_not_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            agent = delivery_with_direction(root, "TASK-ACTIVE")
            agreed_requirements(root, agent["id"], "Final agreed requirements: a command prints a greeting.")
            with board.locked_state(root) as state:
                state.setdefault("difficulty_notes", {})["TASK-ACTIVE"] = [
                    {"role": "builder", "step": "R1", "at": "2026-10-26T10:00:00+00:00", "text": "the build was <img src=x> broken"},
                    {"role": "reviewer", "step": "R1", "at": "2026-10-26T10:05:00+00:00", "text": "the requirement was vague"},
                ]
            result = run_page(self, root, PROBE, width=1200, height=1000, wait=120)
        self.assertTrue(result and "error" not in result, result)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["line"], "What slowed the builder: the build was <img src=x> broken · What slowed the reviewer: the requirement was vague")
        self.assertEqual(result["injected"], 0)


if __name__ == "__main__":
    unittest.main()
