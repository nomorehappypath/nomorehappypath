# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome): a finished task shows a plain "Code quality" summary, and a non-functional requirement in the
contract list is labelled "Quality requirement:". Nothing asks the owner anything; the text is escaped.

Run:  PYTHONPATH=. python3 -m unittest tests.test_code_quality_and_nfr_rendered -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, board_viewer, browser_acceptance, control
from tests.environment_support import require_loopback
from tests.test_frozen_review_scope_rendered import _render
from tests.test_requirements_contract import context, delivery_with_direction

PROBE = r"""
<script>
(async () => {
  const history = document.querySelector('#history');
  if (history && !history.open) history.open = true;
  for (let attempt = 0; attempt < 120; attempt++) {
    if (document.querySelector('.history-item [data-code-quality]')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  document.querySelectorAll('details.history-date-group').forEach(group => { group.open = true; });
  await new Promise(resolve => setTimeout(resolve, 400));
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const quality = document.querySelector('.history-item [data-code-quality]');
  const tags = Array.from(document.querySelectorAll('.history-item [data-quality-requirement]'));
  const first = tags[0] ? tags[0].closest('li') : null;
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    quality: quality ? quality.innerText : null, qualityVisible: visible(quality), injected: document.querySelectorAll('[data-code-quality] img').length,
    tags: tags.length, firstTag: first ? first.innerText : null,
    listed: Array.from(document.querySelectorAll('.history-item .checked-in-advance-list > li')).map(li => li.innerText),
    cards: Array.from(document.querySelectorAll('#owner-actions .owner-action, #owner-actions article')).map(node => node.innerText),
  })});
})();
</script>
"""


class RenderedCodeQualityAndQualityRequirement(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_the_owner_reads_the_code_quality_summary_and_the_quality_requirement_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            session = control.create(root, "claude_cto")
            cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
            task = "TASK-QUALITY"
            delivery_with_direction(root, task)
            with board.locked_state(root) as state:
                state["releases"][task] = {"task": task, "status": "RELEASE_ACCEPTED", "cto_id": cto["id"], "recorded_at": board.now(), "head_commit": "c0ffee"}
                state["release_decisions"][task] = {"task": task, "decision": "accepted", "recorded_at": board.now()}
                state["code_quality_notes"] = {task: {"step": "R1", "at": board.now(), "notes": [
                    "harness/report.py: the totals loop is copied three times", "app.py: a hard-coded port <img src=x>"]}}
                state["requirement_confirmations"] = {task: {
                    "text": "Final agreed requirements: a report page.", "version": 1, "confirmed_at": board.now(),
                    "contract_check": {"labels": [
                        {"n": 1, "criterion": "The report page shows the totals", "plain": "The report shows your totals.", "label": "checked", "kind": "", "coverage": "covered"},
                        {"n": 2, "criterion": "Non-functional: the page loads in under 2 seconds", "plain": "The page opens in under 2 seconds.", "label": "checked", "kind": "non-functional", "coverage": "covered"}],
                        "results": {}}}}
            reading = _render(self, board_viewer.make_handler(root, project_name="Quality proof"), PROBE)
        self.assertTrue(reading["qualityVisible"], reading)
        self.assertIn("Code quality", reading["quality"])
        self.assertIn("nothing for you to do", reading["quality"])
        self.assertIn("harness/report.py: the totals loop is copied three times", reading["quality"])
        self.assertIn("a hard-coded port <img src=x>", reading["quality"], "shown as text")
        self.assertEqual(reading["injected"], 0)
        self.assertEqual(reading["tags"], 2, "only the non-functional requirement is labelled: once in the list, once in its Details")
        self.assertTrue(reading["firstTag"].startswith("Quality requirement: The page opens in under 2 seconds."), reading["firstTag"])
        self.assertFalse(any(item.startswith("Quality requirement") for item in reading["listed"] if "totals" in item))
        self.assertEqual(reading["cards"], [], "nothing asks the owner anything")


if __name__ == "__main__":
    unittest.main()
