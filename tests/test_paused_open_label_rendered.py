# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof: the open paused project says it is the open one; Delete works on it (2026-09-28, Delete 2026-10-28).

Every paused project showed the same "Paused" badge, while Remove is hidden
for the one that is still the open project. The owner saw two identical cards,
one with Remove and one without. The open one now reads "Paused · open" with a
plain hover sentence; closed paused projects keep "Paused".

Run:  PYTHONPATH=. python3 -m unittest tests.test_paused_open_label_rendered -v
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

HOVER = "Still your open project. Delete closes it first, then takes it off the list."

PROBE = r"""
<script>
(async () => {
  for (let i = 0; i < 100 && document.querySelectorAll('#projects .project').length < 3; i++) await new Promise(r => setTimeout(r, 100));
  await new Promise(r => setTimeout(r, 250));
  const cards = Array.from(document.querySelectorAll('#projects .project')).map(card => {
    const badge = card.querySelector('.project-top .badge');
    return {
      name: (card.querySelector('h3')?.textContent || '').trim(),
      badge: (badge?.textContent || '').trim(),
      shown: (badge?.innerText || '').trim(),
      title: badge?.getAttribute('title'),
      badgeClass: badge?.className || '',
      actions: Array.from(card.querySelectorAll('.actions button, .adv-actions button')).map(b => b.textContent.trim()),   // Remove moved into the card's Advanced menu
      visible: card.getBoundingClientRect().height > 0 && (badge?.getBoundingClientRect().width || 0) > 0,
    };
  });
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({cards})});
})();
</script>
"""


class RenderedPausedOpenLabelTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def project(self, home: Path, name: str, *, paused: bool) -> dict:
        code_root = self.base / "code" / name
        code_root.mkdir(parents=True)
        entry = registry.register(home, name, code_root, kind="adopted", description=f"{name} for the label proof")
        if paused:
            context = registry.context_for_entry(entry)
            board.begin_project_pause(context, drain_seconds=0)
            board.finish_project_pause(context)
        return entry

    def test_open_paused_project_reads_paused_open_and_closed_ones_keep_paused(self):
        home = self.base / "home"
        studio = self.project(home, "studio", paused=True)
        self.project(home, "Weather APP", paused=True)
        self.project(home, "Notes", paused=False)
        registry.activate(home, studio["id"])
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
        self.assertEqual(set(cards), {"studio", "Weather APP", "Notes"}, "a blank render is not a pass: " + repr(sink["value"]))
        studio_card, weather, notes = cards["studio"], cards["Weather APP"], cards["Notes"]
        self.assertTrue(all(card["visible"] for card in cards.values()), cards)

        self.assertEqual(studio_card["badge"], "Paused · open")
        self.assertEqual(studio_card["title"], HOVER)
        self.assertIn("paused", studio_card["badgeClass"].split())
        self.assertIn("Delete", studio_card["actions"], "the open project can be deleted too (owner 2026-10-28): it is closed first")
        self.assertIn("Resume project", studio_card["actions"])

        # A project that is not open is not read (owner 2026-10-28), so its card cannot say it is paused; Open still resumes it.
        self.assertEqual(weather["badge"], "Idle")
        self.assertIsNone(weather["title"])
        self.assertIn("Delete", weather["actions"])
        self.assertIn("Open project", weather["actions"])

        self.assertEqual(notes["badge"], "Idle")
        self.assertIsNone(notes["title"])


if __name__ == "__main__":
    unittest.main()
