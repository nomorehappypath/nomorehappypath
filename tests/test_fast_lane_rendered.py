# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome): the owner's Small change choice on the requirements card and the plain line (task 2026-10-26_fast-lane).

Run:  PYTHONPATH=. python3 -m unittest tests.test_fast_lane_rendered -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, browser_acceptance, control
from tests.environment_support import require_loopback
from tests.test_no_terminal_questions_rendered import run_page
from tests.test_requirements_contract import context, delivery_with_direction

PLAIN = "In plain words:\nThe button label says Save instead of Submit.\nNothing else changes.\nNothing for you to do.\n\nObjective: relabel the button."

PROBE = r"""
<script>
(async () => {
  const names = ['TASK-FULL', 'TASK-SMALL'];
  const card = name => document.getElementById('req-decision-' + name);
  for (let attempt = 0; attempt < 150; attempt++) {
    if (names.every(name => card(name))) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const describe = name => {
    const box = card(name);
    const choice = box.querySelector('[data-small-change-choice]');
    const note = box.querySelector('[data-contract-check="small_change"]');
    return {
      choice: choice ? choice.innerText.replace(/\s+/g, ' ').trim() : null,
      checked: choice ? choice.querySelector('input').checked : null,
      note: note ? note.innerText.replace(/\s+/g, ' ').trim() : null,
      goEnabled: Boolean(box.querySelector('[data-req-go]')) && !box.querySelector('[data-req-go]').disabled,
    };
  };
  const out = Object.fromEntries(names.map(name => [name, describe(name)]));
  // tick the box and press Go ahead on the full card: the decision carries small_change true
  const sent = [];
  const realFetch = window.fetch;
  window.fetch = (url, options) => { if (String(url).includes('requirements-decision')) sent.push(JSON.parse(options.body)); return realFetch(url, options); };
  window.confirm = () => true;
  const box = card('TASK-FULL');
  box.querySelector('[data-small-change]').click();
  box.querySelector('[data-req-go]').click();
  for (let attempt = 0; attempt < 40 && !sent.length; attempt++) await new Promise(resolve => setTimeout(resolve, 100));
  out.sent = sent;
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""


class RenderedFastLaneTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_owner_sees_one_plain_choice_and_the_plain_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            session = control.create(root, "claude_reviewer")
            board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
            control.take_instructions(root, session["id"])
            full = delivery_with_direction(root, "TASK-FULL")
            board.record_requirement_proposal(root, full["id"], PLAIN)
            small = delivery_with_direction(root, "TASK-SMALL")
            board.record_requirement_proposal(root, small["id"], PLAIN, small_change=True)
            result = run_page(self, root, PROBE, width=1200, height=1600, wait=120)
        self.assertTrue(result, "Chrome reported nothing")
        full_card, small_card = result["TASK-FULL"], result["TASK-SMALL"]
        self.assertEqual(full_card["choice"], "Small change: skip the question round and the advance test checks")
        self.assertIs(full_card["checked"], False, "the full process is the default")
        self.assertIsNone(full_card["note"])
        self.assertTrue(full_card["goEnabled"])
        self.assertIsNone(small_card["choice"], "Delivery already proposed it: the owner's Go ahead confirms it")
        self.assertIn("Small change: tests are not written in advance.", small_card["note"])
        self.assertIn("Go ahead confirms it", small_card["note"])
        self.assertTrue(small_card["goEnabled"])
        [decision] = result["sent"]
        self.assertEqual((decision["decision"], decision["small_change"]), ("go_ahead", True))


if __name__ == "__main__":
    unittest.main()
