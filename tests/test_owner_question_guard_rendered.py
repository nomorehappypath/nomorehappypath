# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome, the real page) of the owner-question guard, 2026-10-15.

The owner sees: a question card with its deadline and default ("If you do not
answer by …, the CTO will …"), and in "What the harness did for you" the line
"CTO acted: …" after a handed-back question and "Default applied …" after a
deadline passed, instead of a question waiting for them.
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
from unittest.mock import patch

from harness import board, board_viewer, browser_acceptance, control, project_memory
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
  const cards = document.querySelector('#owner-actions');
  const harness = document.querySelector('#harness-actions');
  const reading = {
    rendered: Boolean(rendered()),
    cardsVisible: visible(cards) && !cards.hidden,
    cards: Array.from(document.querySelectorAll('#owner-actions .owner-action')).map(card => ({
      title: (card.querySelector('strong')?.textContent || '').trim(),
      footer: (card.querySelector(':scope > small')?.textContent || '').trim(),
      visible: visible(card),
    })),
    harnessVisible: visible(harness) && !harness.hidden,
    harnessHeading: (harness?.querySelector('h2')?.textContent || '').trim(),
    harnessLines: Array.from(document.querySelectorAll('#harness-actions li')).map(li => ({text: li.textContent.trim(), visible: visible(li)})),
    pageText: document.body.innerText,
  };
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(reading)});
})();
</script>
"""

SAFE_DEFAULT = "continue with deck A as the working candidate"


class RenderedOwnerQuestionGuardTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        project_memory.initialize(self.root, project_name="Owner question guard proof", description="Facts.")
        self.session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=self.session["id"])
        board.configure_owner_questions(None)
        self.addCleanup(board.configure_owner_questions, None)

    def render(self) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Owner question guard proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="owner-question-proof",
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

    def test_the_question_card_shows_its_deadline_and_the_owner_sees_the_cto_acted(self):
        # a handed-back question followed by the CTO's report
        with self.assertRaisesRegex(ValueError, "act, then report"):
            board.status(self.root, self.cto["id"], "Want me to push Delivery to file the amendment now?", "waiting")
        board.status(self.root, self.cto["id"], "Routed Delivery to file the amendment for the waived fresh run.")
        # a real owner question with a default inside the CTO's authority
        card = board.record_owner_action(
            self.root, self.cto["id"], "Which of the two decks should ship?", kind="decision", default=SAFE_DEFAULT, default_minutes=10,
        )
        before = self.render()
        self.assertTrue(before["cardsVisible"], json.dumps(before, indent=2)[:3000])
        titles = {item["title"]: item for item in before["cards"]}
        self.assertIn("Which of the two decks should ship?", titles)
        question = titles["Which of the two decks should ship?"]
        self.assertTrue(question["visible"], "the card has no geometry")
        self.assertIn("If you do not answer by", question["footer"])
        self.assertIn(f"the CTO will {SAFE_DEFAULT}", question["footer"])
        self.assertTrue(before["harnessVisible"], json.dumps(before["harnessLines"], indent=2))
        self.assertEqual(before["harnessHeading"], "What the harness did for you")
        acted = [line for line in before["harnessLines"] if line["text"].startswith("CTO acted: Routed Delivery to file the amendment")]
        self.assertEqual(len(acted), 1, json.dumps(before["harnessLines"], indent=2))
        self.assertTrue(acted[0]["visible"])
        self.assertNotIn("Want me to push Delivery", before["pageText"], "the handed-back question must never reach the owner")

        # the deadline passes: the default is routed and the card leaves, the owner sees what happened
        with board.locked_state(self.root) as state:
            state["owner_actions"][card["id"]]["default_due_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with patch("harness.control.enqueue_instruction", return_value={"id": "d-1", "source": "owner-default"}):
            board.mark_stalled(self.root)
        after = self.render()
        self.assertEqual([item["title"] for item in after["cards"]], [], json.dumps(after["cards"], indent=2))
        applied = [line for line in after["harnessLines"] if line["text"].startswith(f"Default applied (no answer in 10 minutes): the CTO was routed to {SAFE_DEFAULT}")]
        self.assertEqual(len(applied), 1, json.dumps(after["harnessLines"], indent=2))
        self.assertTrue(applied[0]["visible"])


if __name__ == "__main__":
    unittest.main()
