# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof of backlog #11 on the real Projects page (headless Chrome).

One project holds a live repair on a task that is not its newest: its card says
"Needs repair" and names the held task. Another project's only hold is on a
task that was already cancelled: its card does not say "Needs repair".
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, browser_acceptance, project_manager, project_registry as registry
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_stale_holds import REASON, studio_state

PROBE = r"""
<script>
(async () => {
  for (let i = 0; i < 100 && document.querySelectorAll('#projects .project').length < 2; i++) await new Promise(r => setTimeout(r, 100));
  await new Promise(r => setTimeout(r, 250));
  const cards = Array.from(document.querySelectorAll('#projects .project')).map(card => ({
    name: (card.querySelector('h3')?.textContent || '').trim(),
    state: card.className,
    badge: (card.querySelector('.project-top .badge')?.textContent || '').trim(),
    progress: (card.querySelector('.progress > span:last-child')?.textContent || '').replace(/\s+/g, ' ').trim(),
    visible: card.getBoundingClientRect().height > 0,
  }));
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({cards})});
})();
</script>
"""


class RenderedStaleHoldTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def project(self, home: Path, name: str, state: dict) -> None:
        code_root = self.base / "code" / name
        code_root.mkdir(parents=True)
        entry = registry.register(home, name, code_root, kind="adopted", description=f"{name} for the hold proof")
        directory = Path(entry["data_root"]) / "board"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "state.json").write_text(__import__("json").dumps({"agents": {}, "events": [], **state}))

    def test_the_card_names_the_held_task_and_a_stale_hold_is_not_a_repair(self):
        home = self.base / "home"
        home.mkdir()
        self.project(home, "Held studio", studio_state("creative-film"))
        self.project(home, "Stale studio", studio_state("creative-film", cancelled=True))
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
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=1000)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported nothing")
        cards = {card["name"]: card for card in sink["value"]["cards"]}
        self.assertEqual(set(cards), {"Held studio", "Stale studio"}, "a blank render is not a pass: " + repr(sink["value"]))
        held, stale = cards["Held studio"], cards["Stale studio"]
        self.assertTrue(held["visible"] and stale["visible"])
        self.assertEqual(held["badge"], "Needs repair")
        self.assertTrue(held["progress"].startswith("creative-film · Needs repair: "), held["progress"])
        self.assertIn(REASON, held["progress"])
        self.assertNotIn("content-planning", held["progress"])
        self.assertNotEqual(stale["badge"], "Needs repair", stale)
        self.assertNotIn("Needs repair", stale["progress"])
        self.assertNotIn("unhealthy", stale["state"])


if __name__ == "__main__":
    unittest.main()
