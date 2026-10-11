# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Settings in a real browser: Gemini is listed for every role, usable when proven, disabled with its reason when not.

Spec board/tasks/2026-10-07_gemini-provider_SPEC.md T1-1. The page is the real manager surface in headless Chrome.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import browser_acceptance, project_manager
from tests.environment_support import require_loopback
from tests.test_gemini_capabilities import write_agy
from tests.test_project_manager_rendered import proxy_handler

SCRIPT = r"""
<script>
(async () => {
  document.querySelector('button[data-page="settings"]').click();
  for (let i = 0; i < 100 && !document.querySelector('[data-setting-provider]'); i++) await new Promise(r => setTimeout(r, 100));
  const selects = Array.from(document.querySelectorAll('[data-setting-provider]'));
  const box = (node) => { const b = node.getBoundingClientRect(); return {w: b.width, h: b.height}; };
  const options = (select) => Array.from(select.options).map(o => ({value: o.value, text: o.textContent, disabled: o.disabled}));
  const first = selects[0];
  const gemini = Array.from(first.options).find(o => o.value === 'gemini');
  let afterPick = null;
  if (gemini && !gemini.disabled) {
    first.value = 'gemini'; first.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 200));
    const role = first.dataset.settingProvider;
    afterPick = {
      model: Array.from(document.querySelector(`[data-setting-model-choice="${role}"]`).options).map(o => o.value),
      effort: Array.from(document.querySelector(`[data-setting-effort="${role}"]`).options).map(o => o.value),
      test: document.querySelector(`[data-settings-test="${role}"]`).textContent,
      testBox: box(document.querySelector(`[data-settings-test="${role}"]`)),
    };
  }
  await fetch('/__layout_result__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    roles: selects.length, options: selects.map(options), afterPick,
    selectBox: box(first), overflowX: document.scrollingElement.scrollWidth > document.scrollingElement.clientWidth,
  })});
})();
</script>
"""


class SettingsRenderedTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def render(self, agy: Path, width: int = 1280) -> dict:
        home = self.base / f"home-{agy.name}-{width}"
        home.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)}):
            manager = project_manager.ProjectManager(home, board_port=0)
            server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            sink: dict = {}
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), proxy_handler(f"http://127.0.0.1:{server.server_address[1]}", sink, SCRIPT))
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            try:
                with tempfile.TemporaryDirectory() as profile:
                    session = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile),
                                                        width=width, height=900)
                    try:
                        deadline = time.monotonic() + 45
                        while "value" not in sink and time.monotonic() < deadline:
                            time.sleep(0.1)
                    finally:
                        session.close()
            finally:
                proxy.shutdown(); proxy.server_close(); server.shutdown(); server.server_close()
        self.assertIn("value", sink, "Chrome never reported the Settings page")
        return sink["value"]

    def test_proven_gemini_is_selectable_in_every_role_with_its_models_efforts_and_test_button(self):
        reading = self.render(write_agy(self.base / "agy"))
        self.assertEqual(reading["roles"], 3)
        for options in reading["options"]:
            gemini = next(option for option in options if option["value"] == "gemini")
            self.assertEqual(gemini["text"], "Gemini (Antigravity CLI)")
            self.assertFalse(gemini["disabled"])
        picked = reading["afterPick"]
        self.assertIn("gemini-3.1-pro-high", picked["model"])
        self.assertTrue(all(model.startswith("gemini-") or model == "__custom__" for model in picked["model"]))
        self.assertEqual(picked["effort"], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(picked["test"], "Test Gemini connection")
        self.assertGreater(picked["testBox"]["w"], 100)
        self.assertFalse(reading["overflowX"], "the page does not scroll sideways")

    def test_an_unavailable_gemini_is_shown_disabled_with_the_reason_and_cannot_be_picked(self):
        reading = self.render(self.base / "no-agy-here")
        for options in reading["options"]:
            gemini = next(option for option in options if option["value"] == "gemini")
            self.assertTrue(gemini["disabled"])
            self.assertTrue(gemini["text"].startswith("Gemini (Antigravity CLI) — not yet available on this machine: "))
            self.assertIn("not installed", gemini["text"])
            self.assertFalse(next(option for option in options if option["value"] == "claude")["disabled"])
        self.assertIsNone(reading["afterPick"])

    def test_the_settings_page_fits_a_narrow_window(self):
        reading = self.render(write_agy(self.base / "agy"), width=480)
        self.assertFalse(reading["overflowX"])
        self.assertLessEqual(reading["selectBox"]["w"], 480)


def tearDownModule():
    from tests.environment_support import assert_agy_pinned
    assert_agy_pinned()


if __name__ == "__main__":
    unittest.main()
