# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome): the owner's Go ahead card shows each requirement's examples, what his direction asks for that the
requirements leave out, the honest count of real tests, and what his result will be checked for; Go ahead stays enabled.

Task 2026-11-24_tdd-examples-first, Changes 1 to 3 and 5.  Run:  PYTHONPATH=. python3 -m unittest tests.test_examples_first_rendered -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness import board, browser_acceptance, control
from tests.environment_support import require_loopback
from tests.test_examples_first import ASKS, EXAMPLE, RUNS_PROGRAM, check, criterion, submission
from tests.test_no_terminal_questions_rendered import run_page
from tests.test_requirements_contract import context, delivery_with_direction

PROBE = r"""
<script>
(async () => {
  const names = ['TASK-MISSING', 'TASK-FOUND'];
  const card = name => document.getElementById('req-decision-' + name);
  for (let attempt = 0; attempt < 150; attempt++) {
    if (names.every(name => card(name) && card(name).querySelector('[data-real-tests]'))) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const text = node => node ? node.innerText.replace(/\s+/g, ' ').trim() : null;
  const describe = name => {
    const box = card(name);
    if (!box) return null;
    const asks = box.querySelector('[data-owner-asks]');
    const go = box.querySelector('[data-req-go]');
    return {
      asksKind: asks ? asks.dataset.ownerAsks : null,
      asks: text(asks),
      asksAbove: Boolean(asks && go && (asks.compareDocumentPosition(go) & Node.DOCUMENT_POSITION_FOLLOWING)),
      count: text(box.querySelector('[data-real-tests]')),
      result: [...box.querySelectorAll('[data-result-list] li')].map(li => li.textContent.trim()),
      resultTitle: text(box.querySelector('[data-result-list] strong')),
      examples: [...box.querySelectorAll('[data-examples] [data-example]')].map(node => node.textContent.trim()),
      labels: [...box.querySelectorAll('.checked-in-advance-list > li > strong')].map(node => node.textContent.trim()),
      goEnabled: Boolean(go) && !go.disabled && go.getBoundingClientRect().width > 0,
      asked: box.querySelectorAll('[data-cq]').length,
    };
  };
  const out = Object.fromEntries(names.map(name => [name, describe(name)]));
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""
LOUD = "Running it with the argument loud prints HELLO in capital letters."

# The confirmed contract on a task card: how each requirement was tested for real at the end, and the note about late examples.
FINAL_PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 150; attempt++) {
    if (document.querySelector('.task [data-checked-in-advance] [data-final-proof]')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const box = document.querySelector('.task [data-checked-in-advance]');
  const out = {
    rows: box ? [...box.querySelectorAll('.checked-in-advance-list > li')].map(li => li.textContent.replace(/\s+/g, ' ').trim()) : [],
    proof: [...document.querySelectorAll('.task [data-final-proof]')].map(node => node.textContent.trim()),
    walks: [...document.querySelectorAll('.task [data-walk]')].map(node => node.textContent.trim()),
    walked: [...document.querySelectorAll('.task [data-walk-results] li')].map(node => node.textContent.replace(/\s+/g, ' ').trim()),
    picture: await (async () => { const a = document.querySelector('.task [data-walk-picture]'); if (!a) return null; const r = await fetch(a.href); return r.status + ' ' + r.headers.get('content-type'); })(),
    late: (document.querySelector('.task [data-late-inventory]') || {}).textContent || null,
    result: [...document.querySelectorAll('.task [data-result-list] li')].map(li => li.textContent.trim()),
  };
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""


class RenderedExamplesFirstTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_the_card_shows_examples_missing_asks_the_count_and_the_result_list(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = context(base)
            session = control.create(root, "claude_reviewer")
            reviewer = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
            control.take_instructions(root, session["id"])
            counted = criterion([check({"test_hello.py": RUNS_PROGRAM}, runs="python3 hello.py", example=1)])
            later = criterion(text="The greeting can be made loud", examples=[LOUD])
            values = {
                "TASK-MISSING": submission(counted, later, owner_asks=ASKS + [{"ask": "It must also work on my phone", "requirements": []}],
                                           result_must_contain=["The word hello on its own line", "Nothing else printed"]),
                "TASK-FOUND": submission(counted),
            }
            for task, value in values.items():
                agent = delivery_with_direction(root, task)
                board.record_requirement_proposal(root, agent["id"], f"Final agreed requirements for {task}: a command prints a greeting.")
                path = base / f"{task}.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                board.record_contract_check_submission(root, reviewer["id"], task, 1, str(path))
            result = run_page(self, root, PROBE, width=1200, height=1800, wait=120)
        self.assertTrue(result, "Chrome reported nothing")
        missing = result["TASK-MISSING"]
        self.assertEqual(missing["asksKind"], "missing")
        self.assertIn("In your direction, not in the requirements:", missing["asks"])
        self.assertIn("It must also work on my phone", missing["asks"])
        self.assertTrue(missing["asksAbove"], "what the requirements leave out sits above the Go ahead button")
        self.assertEqual(missing["count"], "1 of 2 requirements have a real test before the build; the others are tested for real at the final review.")
        self.assertEqual(missing["resultTitle"], "Your scenario’s result will be checked for:")
        self.assertEqual(missing["result"], ["The word hello on its own line", "Nothing else printed"])
        self.assertEqual(missing["examples"], ["Example: " + EXAMPLE, "Example: " + LOUD])
        self.assertEqual(missing["labels"], ["checked", "not checked in advance"])
        self.assertTrue(missing["goEnabled"], "Go ahead is never held")
        self.assertEqual(missing["asked"], 0, "no new question for the owner")
        found = result["TASK-FOUND"]
        self.assertEqual(found["asksKind"], "found")
        self.assertEqual(found["asks"], "The reviewer found everything you asked for in the requirements (1 points).")
        self.assertEqual(found["count"], "1 of 1 requirements have a real test before the build; the others are tested for real at the final review.")
        self.assertTrue(found["goEnabled"])

    def test_the_task_card_says_how_each_requirement_was_tested_for_real(self):
        from harness import contract_check, examples_first as rules
        from tests.requirements_support import agreed_requirements
        counted = criterion([check({"test_hello.py": RUNS_PROGRAM}, runs="python3 hello.py", example=1)])
        criteria = contract_check.parse_submission(json.dumps(submission(counted, criterion(text="The greeting can be made loud", examples=[LOUD]))))["criteria"]
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            agent = delivery_with_direction(root, "TASK-ACTIVE")
            picture = Path(temporary) / "last.png"
            picture.write_bytes(b"\x89PNG stand-in")
            criteria[0]["checks"] = [{"coverage": "checked", "runs": "the app", "example": 1, "walk": "Open the app. See \u201chello\u201d."}]
            agreed_requirements(root, agent["id"], "Final agreed requirements: a command prints a greeting.")
            with board.locked_state(root) as state:
                state["requirement_confirmations"]["TASK-ACTIVE"]["contract_check"] = {
                    "status": "ready", "reason": "", "rules": 2, "labels": contract_check.labels(criteria), "answers": [], "checks": [],
                    "results": {"1": "passed"}, "compare": {}, "walks": {
                        "1.1": {"status": "passed", "reason": "", "picture": str(picture), "walk": "Open the app. See \u201chello\u201d."},
                        "1.2": {"status": "failed", "reason": "step 2 did not work", "picture": "", "walk": "Open the page /b."}},
                    "late_inventory": True, "result_must_contain": ["The word hello on its own line"],
                    "final_proof": {"1": rules.STORED, "2": "not tested for real: this machine has no loud mode to try"},
                }
            result = run_page(self, root, FINAL_PROBE, width=1200, height=1400, wait=120)
        self.assertTrue(result, "Chrome reported nothing")
        self.assertEqual(result["proof"], [rules.STORED, "not tested for real: this machine has no loud mode to try"])
        self.assertEqual(len(result["rows"]), 2, result["rows"])
        self.assertIn("not tested for real: this machine has no loud mode to try", result["rows"][1])
        self.assertEqual(result["walks"], ["NMHP will try it: Open the app. See \u201chello\u201d."])
        self.assertEqual(len(result["walked"]), 2, result["walked"])
        self.assertIn("passed", result["walked"][0])
        self.assertIn("failed (step 2 did not work)", result["walked"][1])
        self.assertEqual(result["picture"], "200 image/png")
        self.assertIn("you did not approve them before the build", result["late"])
        self.assertEqual(result["result"], ["The word hello on its own line"])


if __name__ == "__main__":
    unittest.main()
