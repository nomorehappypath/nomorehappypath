# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Mission Control in a real browser with Gemini in the roles (spec T1-2, T1-3).

The launch buttons name the provider that runs, and a Gemini agent that needs the owner (sign-in, trust question)
says so in Gemini's words in the waiting banner, never in Claude's ("run /login").
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import attention, board, board_viewer, browser_acceptance, control, global_settings, project_memory
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_gemini_capabilities import write_agy

PROBE = r"""
<script>
(async () => {
  const wait = async (test, tries = 150) => { for (let i = 0; i < tries && !test(); i++) await new Promise(r => setTimeout(r, 100)); return test(); };
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const banner = () => document.querySelector('#waiting-banner');
  await wait(() => banner() && !banner().hidden && /Gemini/.test(banner().textContent) && /Delivery/.test(document.querySelector('#codex')?.textContent || ''));
  const buttons = {};
  for (const id of ['#codex', '#claude', '#cto']) buttons[id] = (document.querySelector(id)?.textContent || '').trim();
  const box = banner().getBoundingClientRect();
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    buttons, banner: banner().innerText, bannerVisible: visible(banner()), bannerRight: box.right, viewport: innerWidth,
    overflowX: document.scrollingElement.scrollWidth > document.scrollingElement.clientWidth,
  })});
})();
</script>
"""


class MissionControlRenderedTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "project"
        self.root.mkdir()
        project_memory.initialize(self.root, project_name="Gemini proof", description="Facts.")
        self.home = Path(self._tmp.name).resolve() / "home"
        agy = write_agy(Path(self._tmp.name).resolve() / "agy")
        patcher = mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def render(self, width: int, cto_reason: str | None = None) -> dict:
        settings = {"delivery": {"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "high"},
                    "cto": {"provider": "gemini", "model": "gemini-3.8-flash-low", "effort": "low"},
                    "reviewer": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"}}
        global_settings.update_agent_settings(self.home, settings)
        delivery = control.create(self.root, "codex_delivery", settings_override=settings)
        cto = control.create(self.root, "claude_cto", settings_override=settings)
        control.record_attention(self.root, delivery["id"], attention.GEMINI_TRUST_REASON)
        control.record_attention(self.root, cto["id"], cto_reason or attention.GEMINI_SIGN_IN_REASON)
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Gemini proof", manager_url="http://127.0.0.1:1/", settings_home=self.home,
            project_id="gemini-proof", chat_action_token="owner-token"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        with tempfile.TemporaryDirectory() as profile:
            process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile), width=width, height=1100)
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and "value" not in sink:
                    time.sleep(0.1)
            finally:
                process.close()
        self.assertIn("value", sink, "Chrome reported nothing: a blank render is not a pass")
        return sink["value"]

    def test_buttons_name_gemini_and_the_banner_speaks_gemini_for_trust_and_sign_in(self):
        reading = self.render(1280)
        dump = json.dumps(reading, indent=2, ensure_ascii=False)
        self.assertTrue(reading["bannerVisible"], dump)
        self.assertTrue(reading["buttons"]["#codex"].startswith("Gemini · gemini-3.1-pro-high · Delivery"), dump)
        self.assertTrue(reading["buttons"]["#claude"].startswith("Codex · "), dump)
        self.assertTrue(reading["buttons"]["#cto"].startswith("Gemini · gemini-3.8-flash-low · CTO"), dump)
        banner = reading["banner"]
        self.assertIn("Gemini is asking whether to trust this project", banner, dump)
        self.assertIn("follow the sign-in prompt in its window", banner, dump)
        self.assertNotIn("/login", banner, "Gemini is never told to run /login")
        self.assertFalse(reading["overflowX"], dump)

    def test_an_agent_out_of_quota_is_told_apart_from_one_needing_sign_in(self):
        quota = attention.detect("⚠ Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 165h0m46s.")
        reading = self.render(1280, cto_reason=quota)
        dump = json.dumps(reading, indent=2, ensure_ascii=False)
        self.assertIn("is out of Gemini quota — open Settings and choose another provider for it, or upgrade your Google plan", reading["banner"], dump)
        self.assertIn("Gemini is asking whether to trust this project", reading["banner"], dump)
        self.assertNotIn("follow the sign-in prompt", reading["banner"], dump)
        self.assertNotIn("/login", reading["banner"], dump)
        self.assertFalse(reading["overflowX"], dump)

    def test_the_banner_fits_a_narrow_window(self):
        reading = self.render(480)
        self.assertLessEqual(reading["bannerRight"], reading["viewport"] + 1)
        self.assertFalse(reading["overflowX"])


def tearDownModule():
    from tests.environment_support import assert_agy_pinned
    assert_agy_pinned()


if __name__ == "__main__":
    unittest.main()
