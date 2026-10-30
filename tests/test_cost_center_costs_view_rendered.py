# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome): the Costs view in Mission Control lists every project and its tasks, newest first, and says when nothing was counted.

Run:  PYTHONPATH=. python3 -m unittest tests.test_cost_center_costs_view_rendered -v
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import browser_acceptance, cost_center, project_manager, project_registry as registry
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_cost_center import CLAUDE_ID, CLAUDE_ID_2, T0, claude_row

PROBE = r"""
<script>
(async () => {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const out = {};
  try {
    document.querySelector('button[data-page="costs"]').click();
    for (let attempt = 0; attempt < 100; attempt++) {
      if (document.querySelectorAll('[data-cost-task]').length >= 2) break;
      await wait(150);
    }
    out.visible = !document.querySelector('#costs-page').hidden;
    out.projects = Array.from(document.querySelectorAll('#costs-list tr.cost-project')).map(card => card.textContent);
    out.tasks = Array.from(document.querySelectorAll('[data-cost-task]')).map(node => node.dataset.costTask);
    out.injected = document.querySelectorAll('#costs-list img').length;
  } catch (error) { out.error = String(error); }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
})();
</script>
"""


class RenderedCostsViewTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        cost_center.configure(None)

    def test_the_owner_sees_every_project_with_its_tasks_newest_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            home = base / "home"
            counted = base / "counted"
            counted.mkdir()
            empty = base / "empty"
            empty.mkdir()
            entry = registry.register(home, "counted-project", counted)
            registry.register(home, "<img src=x>", empty)
            store = base / "claude-home"
            for number, vendor in enumerate((CLAUDE_ID, CLAUDE_ID_2)):
                transcript = store / "projects" / "-synthetic" / f"{vendor}.jsonl"
                transcript.parent.mkdir(parents=True, exist_ok=True)
                transcript.write_text(claude_row(f"msg_{number}", 60 * number + 1, (1000, 500, 2000, 300)), encoding="utf-8")
            board = Path(entry["data_root"]) / "board"
            for number, (vendor, task) in enumerate(((CLAUDE_ID, "OLDER-TASK"), (CLAUDE_ID_2, "NEWER-TASK"))):
                started = T0 + timedelta(hours=number)
                cost_center.collect(board, [{"managed": f"m{number}", "provider": "claude", "id": vendor, "store": str(store), "role": "delivery",
                                             "holder": {"task": task, "spawned_at": started.isoformat()}}], now=started + timedelta(minutes=5))
            manager = project_manager.ProjectManager(home, board_port=0)
            server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            sink: dict = {}
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            self.addCleanup(proxy.server_close)
            self.addCleanup(proxy.shutdown)
            profile = tempfile.TemporaryDirectory()
            self.addCleanup(profile.cleanup)
            process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1300, height=950)
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and "value" not in sink:
                    time.sleep(0.1)
                reading = sink.get("value") or {}
            finally:
                process.close()
        self.assertTrue(reading and "error" not in reading, reading)
        self.assertTrue(reading["visible"])
        self.assertEqual(reading["tasks"], ["NEWER-TASK", "OLDER-TASK"])
        self.assertTrue(any("Project so far: about $0.0159 API-equivalent" in text for text in reading["projects"]), reading["projects"])
        self.assertTrue(any("Nothing counted yet" in text for text in reading["projects"]))
        self.assertEqual(reading["injected"], 0)


if __name__ == "__main__":
    unittest.main()
