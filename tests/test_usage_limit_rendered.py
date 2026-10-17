# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof: a terminal waiting out a usage limit shows one calm line with the time, not a red alarm (2026-10-16_quota-handoff).

Real headless Chrome against the real board page. The line must be visible and readable at desktop and phone width, the red
"agent is waiting for you" banner must stay hidden, and the agent row must say WAITING: USAGE LIMIT rather than NEEDS SIGN-IN or stalled.

Run:  PYTHONPATH=. python3 -m unittest tests.test_usage_limit_rendered -v
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

from harness import attention, board, board_viewer, browser_acceptance, control, project_memory
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy

PROBE = r"""
<script>
(async () => {
  const visible = node => Boolean(node) && !node.hidden && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const notice = () => document.querySelector('#limit-notice');
  for (let attempt = 0; attempt < 150 && !(notice() && !notice().hidden); attempt++) await new Promise(r => setTimeout(r, 100));
  await new Promise(r => setTimeout(r, 400));
  const row = Array.from(document.querySelectorAll('#agents .agent-row')).find(r => (r.textContent || '').includes('Role: engineering') || (r.textContent || '').includes('Delivery'));
  const note = notice();
  const rect = note ? note.getBoundingClientRect() : {left: 0, right: 0};
  const reading = {
    noticeVisible: visible(note), noticeText: note ? note.textContent.trim() : '',
    noticeFits: rect.left >= 0 && rect.right <= window.innerWidth + 1,
    bannerHidden: document.querySelector('#waiting-banner')?.hidden !== false,
    bannerText: (document.querySelector('#waiting-banner')?.textContent || '').trim(),
    badges: Array.from(document.querySelectorAll('#agents .badge')).map(n => n.textContent.trim()),
    rowText: row ? row.textContent.trim() : '',
    pageFits: document.documentElement.scrollWidth <= window.innerWidth + 1,
    noticeFontPx: note ? parseFloat(getComputedStyle(note).fontSize) : 0,
  };
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(reading)});
})();
</script>
"""


class RenderedUsageLimitTests(unittest.TestCase):
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
        project_memory.initialize(self.root, project_name="Usage limit proof", description="Facts.")
        self.session = control.create(self.root, "codex_delivery")
        self.agent = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=self.session["id"])

    def hold(self, resets_in_hours: float = 3.0, kind: str = "announced") -> None:
        seen = datetime.now().astimezone()
        control.record_attention(self.root, self.session["id"], attention.limit_reason("codex"), limit={
            "seen_at": seen.isoformat(timespec="seconds"), "resets_at": (seen + timedelta(hours=resets_in_hours)).isoformat(timespec="seconds"),
            "kind": kind, "when_text": "9:43pm", "provider": "codex"})
        board.mark_stalled(self.root)

    def render(self, width: int = 1280, height: int = 1000) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Usage limit proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="usage-limit-proof", chat_action_token="token"))
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
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=width, height=height)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        return sink["value"]

    def check_waiting_form(self, reading: dict) -> None:
        detail = json.dumps(reading, indent=2)
        self.assertTrue(reading["noticeVisible"], detail)
        self.assertTrue(reading["noticeFits"] and reading["pageFits"], detail)
        self.assertIn("The Delivery agent (Codex) shows Codex's usage-limit message; it announced", reading["noticeText"], detail)
        self.assertIn("If this is not a real limit, type a message into its terminal", reading["noticeText"], detail)
        self.assertGreaterEqual(reading["noticeFontPx"], 14, detail)
        self.assertTrue(reading["bannerHidden"], "the red 'waiting for you' banner must stay hidden: " + detail)
        self.assertNotIn("WAITING FOR YOU", reading["badges"], detail)
        self.assertNotIn("NEEDS SIGN-IN", reading["badges"], detail)
        self.assertIn("WAITING: USAGE LIMIT", reading["badges"], detail)

    def test_desktop_shows_one_calm_line_and_no_alarm(self):
        self.hold()
        self.check_waiting_form(self.render(1280, 1000))

    def test_a_phone_width_shows_the_same_line_without_horizontal_scroll(self):
        self.hold()
        self.check_waiting_form(self.render(390, 900))

    def test_the_estimated_form_and_the_still_out_form_read_plainly(self):
        self.hold(resets_in_hours=1.0, kind="estimated")
        self.assertIn("(estimated)", self.render()["noticeText"])
        self.hold(resets_in_hours=-1.0)
        control.record_limit_nudge(self.root, self.session["id"], sent=True)
        with control.locked_state(self.root) as state:
            state["sessions"][self.session["id"]]["limit_nudged_at"] = (datetime.now(timezone.utc) - timedelta(seconds=attention.LIMIT_NUDGE_AFTER_SECONDS + 30)).isoformat()
        reading = self.render()
        self.assertIn("but still shows Codex's usage-limit message", reading["noticeText"])
        self.assertIn("It keeps waiting.", reading["noticeText"])

    def test_without_a_limit_the_notice_is_absent(self):
        control.record_attention(self.root, self.session["id"], attention.PERMISSION_REASON)
        server_reading = self.render_without_notice()
        self.assertFalse(server_reading["noticeVisible"], json.dumps(server_reading, indent=2))
        self.assertFalse(server_reading["bannerHidden"], "a real prompt still raises the owner's banner")

    def render_without_notice(self) -> dict:
        # The probe waits for the notice; with none it times out after ~15 s and reports what is there.
        return self.render()


if __name__ == "__main__":
    unittest.main()
