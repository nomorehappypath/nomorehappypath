# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Settings: Delivery and Reviewer never end up on one AI company, and a refused Save is plainly visible.

Task 2026-10-26_settings-role-vendor. Owner: picked Codex as Delivery while the Reviewer was Codex, Save was refused
unseen, and the project kept proposing the old roles. Real manager page in headless Chrome, the test's own temp home.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import browser_acceptance, global_settings, project_manager
from tests.environment_support import require_loopback
from tests.test_project_manager_rendered import proxy_handler

SCRIPT = r"""
<script>
(async () => {
  const q = s => document.querySelector(s);
  const wait = async (t) => { for (let i = 0; i < 300 && !t(); i++) await new Promise(r => setTimeout(r, 100)); };
  const pick = (role, value) => { const s = q(`[data-setting-provider="${role}"]`); s.value = value; s.dispatchEvent(new Event('change')); };
  const read = () => ({
    delivery: q('[data-setting-provider="delivery"]').value, reviewer: q('[data-setting-provider="reviewer"]').value,
    cto: q('[data-setting-provider="cto"]').value,
    reviewerNote: (q('[data-settings-switch="reviewer"]') && !q('[data-settings-switch="reviewer"]').hidden) ? q('[data-settings-switch="reviewer"]').textContent : '',
    deliveryNote: (q('[data-settings-switch="delivery"]') && !q('[data-settings-switch="delivery"]').hidden) ? q('[data-settings-switch="delivery"]').textContent : '',
    reviewerModels: [...q('[data-setting-model-choice="reviewer"]').options].map(o => o.value),
    error: q('#settings-error').textContent, errorVisible: q('#settings-error').getBoundingClientRect().height > 0,
    status: q('#status').hidden ? '' : q('#status').textContent,
  });
  const out = {};
  q('button[data-page="settings"]').click();
  await wait(() => q('[data-setting-provider]'));
  out.start = read();
  pick('delivery', 'codex');
  out.afterPick = read();
  q('#settings-save').click();
  await wait(() => /saved/.test(q('#status').textContent));
  out.afterSave = read();
  // A refused Save: force both roles onto one company without the page's own switch, then press Save.
  q('[data-setting-provider="delivery"]').value = 'claude';
  q('[data-setting-provider="reviewer"]').value = 'claude';
  q('#status').hidden = true;
  q('#settings-save').click();
  await wait(() => q('#settings-error').textContent);
  out.refused = read();
  await fetch('/__layout_result__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""


class SettingsRoleVendorRenderedTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def test_choosing_the_reviewers_company_for_delivery_moves_the_reviewer_aside_and_saves(self):
        start = {"provider": "claude", "model": "", "effort": ""}
        base = global_settings.load(self.home)["agent_settings"]
        seeded = {role: dict(base[role]) for role in base}
        seeded["delivery"] = {"provider": "claude", "model": "claude-sonnet-5-5", "effort": "medium"}
        seeded["reviewer"] = {"provider": "codex", "model": "gpt-5.6-terra", "effort": "medium"}
        global_settings.update_agent_settings(self.home, seeded)
        manager = project_manager.ProjectManager(self.home, board_port=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), proxy_handler(f"http://127.0.0.1:{server.server_address[1]}", sink, SCRIPT))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as profile:
                session = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile), width=1280, height=900)
                try:
                    deadline = time.monotonic() + 60
                    while "value" not in sink and time.monotonic() < deadline:
                        time.sleep(0.1)
                finally:
                    session.close()
        finally:
            proxy.shutdown(); proxy.server_close(); server.shutdown(); server.server_close()
        self.assertIn("value", sink, "Chrome never reported the Settings page")
        out = sink["value"]
        self.assertEqual((out["start"]["delivery"], out["start"]["reviewer"]), ("claude", "codex"))
        cto = out["start"]["cto"]
        after = out["afterPick"]
        self.assertEqual((after["delivery"], after["reviewer"], after["cto"]), ("codex", "claude", cto))
        self.assertIn("Independent reviewer switched to Claude, so your work is always checked by a different AI company. Save to keep it.", after["reviewerNote"])
        self.assertTrue(after["reviewerModels"] and not any(m.startswith("gpt-") for m in after["reviewerModels"]))
        saved = global_settings.load(self.home)["agent_settings"]
        self.assertEqual((saved["delivery"]["provider"], saved["reviewer"]["provider"], saved["cto"]["provider"]), ("codex", "claude", cto))
        self.assertIn("saved", out["afterSave"]["status"])
        # A refused Save is shown beside the button and in the page status, in plain words, and nothing is stored.
        refused = out["refused"]
        self.assertIn("Not saved", refused["error"])
        self.assertIn("different AI companies", refused["error"])
        self.assertTrue(refused["errorVisible"])
        self.assertIn("were not saved", refused["status"])
        self.assertNotIn("settings saved", refused["status"])
        saved = global_settings.load(self.home)["agent_settings"]
        self.assertEqual((saved["delivery"]["provider"], saved["reviewer"]["provider"]), ("codex", "claude"))


if __name__ == "__main__":
    unittest.main()
