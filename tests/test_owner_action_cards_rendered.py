# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof of backlog #9: owner-action cards clear themselves.

Read in headless Chrome from the real Mission Control page: each card says
what will clear it, a task's card leaves the page when the task is accepted,
an expired card is gone, and the section disappears when nothing is left.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance, control, project_memory
from tests import test_board as _board_tests
from tests import test_reviewer_release as _release_tests
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy

PROBE = r"""
<script>
(async () => {
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const rendered = () => document.querySelector('#agents .agent-row');
  for (let attempt = 0; attempt < 150 && !rendered(); attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  await new Promise(resolve => setTimeout(resolve, 400));
  const section = document.querySelector('#owner-actions');
  const reading = {
    rendered: Boolean(rendered()),
    sectionVisible: visible(section) && !section.hidden,
    heading: (section?.querySelector('h2')?.textContent || '').trim(),
    cards: Array.from(document.querySelectorAll('#owner-actions .owner-action')).map(card => ({
      title: (card.querySelector('strong')?.textContent || '').trim(),
      footer: (card.querySelector(':scope > small')?.textContent || '').trim(),
      visible: visible(card),
    })),
    pageText: document.body.innerText,
  };
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(reading)});
})();
</script>
"""


class RenderedOwnerActionCardTests(unittest.TestCase):
    maxDiff = None
    ledger = _board_tests.BoardTests.ledger
    delivery = _board_tests.BoardTests.delivery
    accept = _release_tests.AcceptanceTests.accept

    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "test_smoke.py").write_text("import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(True)\n")
        project_memory.initialize(self.root, project_name="Owner cards proof", description="Facts.")
        session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])

    def render(self) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Owner cards proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="owner-cards-proof",
            chat_action_token="owner-token",
        ))
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
        self.assertIn("value", sink, "Chrome reported no probe")
        reading = sink["value"]
        self.assertTrue(reading["rendered"], "the page never rendered its agents; a blank render is not a pass")
        return reading

    def pin(self, title: str, **kwargs) -> dict:
        return board.record_owner_action(self.root, self.cto["id"], title, **kwargs)

    def test_each_card_says_what_clears_it_and_the_task_card_leaves_on_accept(self):
        self.delivery("SHIP")
        self.pin("SHIP is release-ready. Open Mission Control and click Accept.", task="SHIP")
        self.pin("Run the film GPU proof", command="bash /tmp/release_film.sh")
        before = self.render()
        self.assertTrue(before["sectionVisible"], json.dumps(before, indent=2)[:3000])
        self.assertEqual(before["heading"], "2 things the CTO needs you to do")
        footers = {card["title"]: card["footer"] for card in before["cards"]}
        self.assertTrue(all(card["visible"] for card in before["cards"]), "a card has no geometry")
        self.assertIn("Clears when the task is accepted.", footers["SHIP is release-ready. Open Mission Control and click Accept."])
        self.assertIn("Expires in 24 h.", footers["Run the film GPU proof"])
        self.assertNotIn("clears when the CTO records the outcome", before["pageText"])

        self.accept("SHIP")
        after = self.render()
        self.assertEqual([card["title"] for card in after["cards"]], ["Run the film GPU proof"], json.dumps(after["cards"], indent=2))
        self.assertEqual(after["heading"], "One thing the CTO needs you to do")

    def test_an_expired_card_is_gone_and_the_empty_section_disappears(self):
        card = self.pin("Run the film GPU proof", command="bash /tmp/release_film.sh")
        with board.locked_state(self.root) as state:
            state["owner_actions"][card["id"]]["recorded_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        unswept = self.render()
        self.assertEqual(unswept["cards"], [], "an expired card is not shown even before the sweep")
        self.assertFalse(unswept["sectionVisible"])
        board.mark_stalled(self.root)
        self.assertEqual(board.snapshot(self.root)["owner_actions"][card["id"]]["outcome"], "Expired: no longer current")
        swept = self.render()
        self.assertFalse(swept["sectionVisible"], json.dumps(swept, indent=2)[:3000])
        self.assertNotIn("things the CTO needs you to do", swept["pageText"])
        self.assertNotIn("thing the CTO needs you to do", swept["pageText"])


if __name__ == "__main__":
    unittest.main()
