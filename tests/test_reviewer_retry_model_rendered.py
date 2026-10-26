# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome, both platforms) of the Settings field "If the review can't be completed, retry once with"
(task 2026-10-25_reviewer-retry-model). Machine inputs removed: nothing is read from the machine; the Settings home is a temporary folder.

Run:  PYTHONPATH=. python3 -m unittest tests.test_reviewer_retry_model_rendered -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import browser_acceptance, control, global_settings, project_manager
from tests.environment_support import require_loopback
from tests.test_frozen_review_scope_rendered import _render

PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 600; attempt++) {
    if (document.querySelector('[data-reviewer-retry-model="claude"]')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const card = document.querySelector('[data-recovery-card]');
  const select = document.querySelector('[data-reviewer-retry-model="claude"]');
  const label = select ? document.querySelector(`label[for="${select.id}"]`) : null;
  const box = select ? select.getBoundingClientRect() : {width: 0, height: 0};
  const result = {
    text: card ? card.innerText : '', visible: box.width > 0 && box.height > 0, label: label ? label.innerText : '',
    options: select ? Array.from(select.options).map(option => option.textContent) : [],
    vendors: Array.from(document.querySelectorAll('[data-reviewer-retry-model]')).map(node => node.dataset.reviewerRetryModel),
    deliveryFieldStillThere: document.querySelectorAll('[data-recovery-model]').length,
  };
  // choose a model, press Save and capture what the page would send (the request is intercepted: nothing is saved)
  const chosen = select && select.options.length > 2 ? select.options[2].value : '';
  if (select) select.value = chosen;
  const realFetch = window.fetch;
  window.fetch = async (url, options) => {
    if (String(url).includes('/api/settings') && options && options.method === 'POST') { result.sent = JSON.parse(options.body); result.chosen = chosen; return new Response('{"error":"stopped for the test"}', {status: 400, headers: {'Content-Type': 'application/json'}}); }
    return realFetch(url, options);
  };
  document.querySelector('#settings-save').click();
  await new Promise(resolve => setTimeout(resolve, 800));
  await realFetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(result)});
})();
</script>
"""


class RenderedReviewerRetryField(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def test_settings_shows_the_review_retry_field_per_vendor_beside_the_repair_one_and_sends_it_with_the_form(self):
        home = Path(self.temporary.name) / "home"
        manager = project_manager.ProjectManager(home, board_port=0)
        reading = _render(self, project_manager.make_handler(manager), PROBE, path="/?page=settings", timeout=180)
        self.assertTrue(reading["visible"], reading)
        self.assertEqual(reading["label"], "If the review can't be completed, retry once with (Claude)")
        self.assertIn("If repairs keep failing", reading["text"], "the Delivery field is still there")
        self.assertIn("When a review cannot be completed", reading["text"])
        self.assertEqual(sorted(reading["vendors"]), ["claude", "codex", "gemini"])
        self.assertEqual(reading["deliveryFieldStillThere"], 3)
        self.assertIn(f"Default: {control.STRONGEST_MODELS['claude']}", reading["options"])
        self.assertTrue(reading["chosen"])
        self.assertEqual(reading["sent"]["reviewer_retry_models"]["claude"], reading["chosen"], "the choice travels with the Settings form")
        self.assertEqual(global_settings.load(home)["reviewer_retry_models"], {"claude": "", "codex": "", "gemini": ""}, "nothing was chosen by viewing")


if __name__ == "__main__":
    unittest.main()
