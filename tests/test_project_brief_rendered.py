# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (task 2026-11-08_project-brief, criterion 7): the owner's Go ahead card shows "Already in the project" under the plain words.

Two projects: one proposal with the second block, and one stored without it (older text), which must still render with nothing extra.
Run:  PYTHONPATH=. python3 -m unittest tests.test_project_brief_rendered -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, browser_acceptance
from tests.environment_support import require_loopback
from tests.test_no_terminal_questions_rendered import run_page
from tests.test_requirements_contract import context, delivery_with_direction

PLAIN = "In plain words:\nA forecast page will show the next five days.\nNothing for you to do."
FIT = "Already in the project:\nThe weather page you accepted already exists.\nThis change adds five days to it."
FULL = "Objective: five-day forecast.\nAcceptance: each day shows a high and a low."

PROBE = r"""
<script>
(async () => {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const out = {errors: []};
  window.addEventListener('error', event => out.errors.push(String(event.message)));
  try {
    for (let attempt = 0; attempt < 150; attempt++) { if (document.querySelector('.requirements-proposal [data-plain-summary]')) break; await wait(150); }
    const card = document.querySelector('.requirements-proposal');
    const plain = card.querySelector('[data-plain-summary]'), fit = card.querySelector('[data-fit-summary]');
    out.plain = plain.textContent.replace(/\s+/g, ' ');
    out.fit = fit ? fit.textContent.replace(/\s+/g, ' ') : '';
    out.fitAfterPlain = Boolean(fit) && Boolean(plain.compareDocumentPosition(fit) & Node.DOCUMENT_POSITION_FOLLOWING);
    out.fitBeforeFull = Boolean(fit) && Boolean(fit.compareDocumentPosition(card.querySelector('details.full-requirements')) & Node.DOCUMENT_POSITION_FOLLOWING);
  } catch (error) { out.error = String(error); }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""


class RenderedFitBlockTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def page(self, text):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            agent = delivery_with_direction(root, "TASK-FIT")
            board.record_requirement_proposal(root, agent["id"], text)
            result = run_page(self, root, PROBE, width=1100, height=900, wait=90)
        self.assertTrue(result and "error" not in result, result)
        self.assertEqual(result["errors"], [])
        return result

    def test_the_block_shows_under_the_plain_words_and_above_the_full_text(self):
        result = self.page(f"{PLAIN}\n\n{FIT}\n\n{FULL}")
        self.assertIn("A forecast page will show the next five days.", result["plain"])
        self.assertIn("Already in the project", result["fit"])
        self.assertIn("The weather page you accepted already exists.", result["fit"])
        self.assertTrue(result["fitAfterPlain"] and result["fitBeforeFull"], result)

    def test_text_stored_without_the_block_shows_nothing_extra(self):
        result = self.page(f"{PLAIN}\n\n{FULL}")
        self.assertEqual(result["fit"], "")
        self.assertIn("A forecast page will show the next five days.", result["plain"])


if __name__ == "__main__":
    unittest.main()
