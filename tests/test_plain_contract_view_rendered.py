# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (task 2026-10-24_plain-contract-view): the owner reads plain words first, in every project.

Two different fixture projects. Project A has new-style data (a plain block, plain sentences, an owner's answer). Project B holds what
older projects already stored (no plain block, no plain sentences): it must render safely, with a plain notice and the details behind toggles.
Run:  PYTHONPATH=. python3 -m unittest tests.test_plain_contract_view_rendered -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness import board, browser_acceptance, control
from tests.environment_support import require_loopback
from tests.requirements_support import agreed_requirements
from tests.test_contract_first_checks import TEST_FILE, ledger
from tests.test_no_terminal_questions_rendered import run_page
from tests.test_requirements_contract import context, delivery_with_direction

BLOCK = "In plain words:\nA forecast page will show the next five days.\nYou will see a high and a low for each day.\nNothing for you to do."
DEVELOPER = "Objective: implement delta 3 of spec section 15 in forecast_view.py (commit 4efdf2f).\nAcceptance: GIVEN a city WHEN opened THEN five days."
SENTENCE = "You always see five days of weather, each with a high and a low."

PROBE = r"""
<script>
(async () => {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const out = {errors: []};
  window.addEventListener('error', event => out.errors.push(String(event.message)));
  try {
    for (let attempt = 0; attempt < 150; attempt++) { if (document.querySelector('.requirements-confirmation:not(.requirements-proposal) [data-plain-summary]') && document.querySelector('.requirements-proposal [data-plain-summary]')) break; await wait(150); }
    const card = selector => document.querySelector(selector);
    const confirmed = card('.requirements-confirmation:not(.requirements-proposal)'), proposal = card('.requirements-proposal');
    const visible = element => Boolean(element) && (typeof element.checkVisibility === 'function' ? element.checkVisibility() : element.getBoundingClientRect().height > 0);
    const describe = box => ({plain: box.querySelector('[data-plain-summary]').textContent.replace(/\s+/g, ' '), kind: box.querySelector('[data-plain-summary]').dataset.plainSummary,
      fullOpen: box.querySelector('details.full-requirements').open, fullVisible: visible(box.querySelector('details.full-requirements .requirements-body')),
      summaryText: box.querySelector('details.full-requirements summary').textContent,
      choices: box.querySelector('[data-plain-choices]') ? box.querySelector('[data-plain-choices]').textContent.replace(/\s+/g, ' ') : ''});
    out.confirmed = describe(confirmed); out.proposal = proposal ? describe(proposal) : null;
    const tested = card('[data-checked-in-advance]');
    out.testedLines = tested ? [...tested.querySelectorAll('.checked-in-advance-list li')].map(li => li.textContent) : [];
    out.testedDetailsOpen = tested ? tested.querySelector('details.tested-details').open : null;
    out.testedDetails = tested ? tested.querySelector('details.tested-details').textContent.replace(/\s+/g, ' ') : '';
    const task = card('.task');
    out.headText = task.querySelector('.task-head').textContent.replace(/\s+/g, ' ');
    const technical = task.querySelector('details.technical-details');
    out.technical = {exists: Boolean(technical), open: technical && technical.open, text: technical ? technical.textContent.replace(/\s+/g, ' ') : '', keyVisible: visible(task.querySelector('.task-key'))};
    const rest = task.cloneNode(true); rest.querySelectorAll('details.technical-details').forEach(node => node.remove());
    out.mainHasJargon = /\batomic\b|Task: |Product Management structure/.test(rest.textContent);
    // a toggle the owner opened stays open across refreshes
    const toggle = confirmed.querySelector('details.full-requirements'); toggle.open = true;
    await wait(5500);
    out.openKept = Boolean(document.querySelector('.requirements-confirmation:not(.requirements-proposal) details.full-requirements').open);
  } catch (error) { out.error = String(error); }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""


class RenderedPlainContractTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def reviewer_ready(self, root):
        session = control.create(root, "claude_reviewer")
        reviewer = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        control.take_instructions(root, session["id"])
        return reviewer

    def reviewed(self, root, agent, task, with_plain, reviewer):
        check = {"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE}}
        value = {"criteria": [{"text": "The spec's section 15 coverage table holds", "plain": SENTENCE, "checks": [check]}],
                 "questions": [{"text": "Which temperature unit should it show?", "options": ["Celsius", "Fahrenheit"], "recommended": 0, "affects": []}]}
        path = Path(tempfile.mkdtemp(dir=str(root.parent))) / "s.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        board.record_contract_check_submission(root, reviewer["id"], task, 1, str(path))
        token = board.snapshot(root)["requirement_proposals"][task]["contract_check"]["token"]
        board.record_requirements_decision(root, task, "go_ahead", version=1, token=token,
                                           answers=[{"question": 1, "choice": 1, "other": ""}])
        if not with_plain:                      # what older projects stored: no sentence per requirement
            with board.locked_state(root) as state:
                for criterion in state["requirement_proposals"][task]["contract_check"]["criteria"]:
                    criterion.pop("plain", None)
        board.record_requirement_confirmation(root, agent["id"], board.snapshot(root)["requirement_proposals"][task]["text"])
        if not with_plain:
            with board.locked_state(root) as state:
                for item in state["requirement_confirmations"][task]["contract_check"]["labels"]:
                    item.pop("plain", None)

    def test_new_style_project_reads_in_plain_words_with_details_behind_toggles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            reviewer = self.reviewer_ready(root)
            agent = delivery_with_direction(root, "TASK-NEW")
            board.record_requirement_proposal(root, agent["id"], BLOCK + "\n\n" + DEVELOPER)
            self.reviewed(root, agent, "TASK-NEW", True, reviewer)
            waiting = delivery_with_direction(root, "TASK-WAIT")
            board.record_requirement_proposal(root, waiting["id"], BLOCK + "\n\n" + DEVELOPER)
            result = run_page(self, root, PROBE, width=1100, height=900, wait=90)
        self.assertTrue(result and "error" not in result, result)
        self.assertEqual(result["errors"], [])
        confirmed = result["confirmed"]
        self.assertEqual(confirmed["kind"], "plain")
        self.assertIn("A forecast page will show the next five days.", confirmed["plain"])
        self.assertIn("Your choices", confirmed["choices"])
        self.assertIn("Fahrenheit", confirmed["choices"])
        self.assertFalse(confirmed["fullOpen"] or confirmed["fullVisible"], "the developer text is behind the toggle")
        self.assertEqual(confirmed["summaryText"], "Full requirements")
        self.assertTrue(any(SENTENCE in line for line in result["testedLines"]), result["testedLines"])
        self.assertFalse(any("section 15" in line for line in result["testedLines"]), "developer wording is not in the list")
        self.assertIn("section 15", result["testedDetails"])
        self.assertFalse(result["testedDetailsOpen"])
        self.assertNotIn("atomic", result["headText"])
        self.assertNotIn("Task:", result["headText"])
        self.assertTrue(result["technical"]["exists"] and not result["technical"]["open"] and not result["technical"]["keyVisible"])
        self.assertRegex(result["technical"]["text"], r"Task: TASK-(NEW|WAIT)")
        self.assertIn("atomic", result["technical"]["text"])
        self.assertFalse(result["mainHasJargon"])
        self.assertTrue(result["openKept"], "a toggle the owner opened stays open across refreshes")

    def test_older_stored_data_in_another_project_renders_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            reviewer = self.reviewer_ready(root)
            agent = delivery_with_direction(root, "TASK-OLD")
            board.record_requirement_proposal(root, agent["id"], DEVELOPER)          # no plain block, as stored by older projects
            self.reviewed(root, agent, "TASK-OLD", False, reviewer)
            waiting = delivery_with_direction(root, "TASK-WAIT")
            board.record_requirement_proposal(root, waiting["id"], DEVELOPER)
            result = run_page(self, root, PROBE, width=1100, height=900, wait=90)
        self.assertTrue(result and "error" not in result, result)
        self.assertEqual(result["errors"], [])
        confirmed = result["confirmed"]
        self.assertEqual(confirmed["kind"], "legacy")
        self.assertIn("written in developer language", confirmed["plain"])
        self.assertEqual(confirmed["summaryText"], "Full requirements (developer language)")
        self.assertFalse(confirmed["fullVisible"])
        self.assertTrue(any("A check the developer wrote; open Details to read it." in line for line in result["testedLines"]), result["testedLines"])
        self.assertIn("section 15", result["testedDetails"])


if __name__ == "__main__":
    unittest.main()
