# SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome) of the Settings colour choice under each role (issue #92): every role card offers it, it starts on
that role's default colour, the swatch follows the choice, and the choice is sent with the Settings form. Machine inputs removed:
nothing is read from the machine; the Settings home is a temporary folder.

Run:  PYTHONPATH=. python3 -m unittest tests.test_role_colors_rendered -v
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
    if (document.querySelector('[data-setting-color="cto"]')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const result = {roles: {}};
  for (const select of document.querySelectorAll('[data-setting-color]')) {
    const role = select.dataset.settingColor, label = document.querySelector(`label[for="${select.id}"]`);
    result.roles[role] = {selected: select.value, label: label ? label.innerText.trim() : '', inCard: !!select.closest(`[data-settings-role="${role}"]`),
      options: Array.from(select.options).map(option => option.textContent), visible: select.getBoundingClientRect().height > 0,
      swatch: getComputedStyle(document.querySelector(`[data-setting-color-swatch="${role}"]`)).backgroundColor};
  }
  const cto = document.querySelector('[data-setting-color="cto"]');
  const before = document.querySelector('[data-settings-result="cto"]').textContent;
  cto.value = 'blue'; cto.dispatchEvent(new Event('change'));
  result.swatchAfter = getComputedStyle(document.querySelector('[data-setting-color-swatch="cto"]')).backgroundColor;
  result.connectionUntouched = document.querySelector('[data-settings-result="cto"]').textContent === before;
  // press Save and capture what the page would send (the request is intercepted: nothing is saved)
  const realFetch = window.fetch;
  window.fetch = async (url, options) => {
    if (String(url).includes('/api/settings') && options && options.method === 'POST') { result.sent = JSON.parse(options.body); return new Response('{"error":"stopped for the test"}', {status: 400, headers: {'Content-Type': 'application/json'}}); }
    return realFetch(url, options);
  };
  document.querySelector('#settings-save').click();
  await new Promise(resolve => setTimeout(resolve, 800));
  await realFetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(result)});
})();
</script>
"""


class RenderedRoleColorChoice(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def test_each_role_card_offers_a_color_set_to_its_default_and_sends_the_choice_with_the_form(self):
        home = Path(self.temporary.name) / "home"
        manager = project_manager.ProjectManager(home, board_port=0)
        reading = _render(self, project_manager.make_handler(manager), PROBE, path="/?page=settings", timeout=180)
        labels = [choice["label"] for choice in control.ROLE_COLOR_CHOICES.values()]
        self.assertEqual(sorted(reading["roles"]), ["cto", "delivery", "reviewer"], "one colour choice under each role")
        for role, default in control.DEFAULT_ROLE_COLORS.items():
            seen = reading["roles"][role]
            self.assertTrue(seen["visible"] and seen["inCard"], seen)
            self.assertEqual(seen["selected"], default, f"{role} starts on its default colour")
            self.assertEqual(seen["label"], "Color in Mission Control")
            self.assertEqual(len(seen["options"]), len(labels))
            self.assertIn(f"{control.ROLE_COLOR_CHOICES[default]['label']} (default)", seen["options"])
        self.assertEqual(reading["roles"]["cto"]["swatch"], "rgb(139, 124, 246)", "the swatch shows the chosen colour")
        self.assertEqual(reading["swatchAfter"], "rgb(59, 130, 246)", "the swatch follows a new choice")
        self.assertTrue(reading["connectionUntouched"], "a colour is display only: no new connection test is asked for")
        self.assertEqual(reading["sent"]["role_colors"], {"delivery": "teal", "reviewer": "coral", "cto": "blue"}, "the choice travels with the form")
        self.assertEqual(global_settings.load(home)["role_colors"], control.DEFAULT_ROLE_COLORS, "nothing was chosen by viewing")


if __name__ == "__main__":
    unittest.main()
